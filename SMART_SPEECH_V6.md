# Smart Speech V6

`POST /smart-speech-v6` runs Russian speech recognition locally with
`vosk==0.3.45` and `vosk-model-small-ru-0.22`. No paid API, cloud transcription,
GPU or external Vosk service is used by V6. Existing V5 remains available.

Upload the same file to V5 and V6 to compare. Add `?diagnostics_only=true` to
V6 for JSON containing recognized words, confidence scores, proposed deletion
timecodes and the `automatic` flag. MP4 responses include proposed deletions in
`X-Smart-Speech-Report`.

## Model installation and the previous HTTP 403

The previous failure was an HTTPS proxy `CONNECT 403`, before reaching the
model server. During this change the same official URL returned HTTP 200 and
the full archive downloaded successfully. Retries cannot repair a network
allowlist denial: allow `alphacephei.com` in the environment's HTTPS egress
settings. Do not turn off TLS verification or switch to HTTP.

```sh
python -m pip install -r requirements.txt -r requirements-vosk.txt
python download_vosk_model.py
```

The official source is
<https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip>.
The installer pins the SHA-256 of the archive fetched over verified HTTPS:
`961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4`.
The archive is 46,236,750 bytes (about 44 MiB). Extracted files have separate
pinned checksums in `vosk_model_files.json`.

The installer:

- streams downloads with timeouts and up to three attempts for transient
  connection failures, HTTP 429 and selected 5xx errors;
- fails clearly on HTTP/proxy 403, checksum failure or a non-HTTPS redirect;
- validates archive size, expected paths, symlinks and extracted file hashes;
- installs from a temporary directory, serializes concurrent installers and
  preserves an existing installation if its replacement fails;
- verifies cached files before reuse, repairs incomplete/corrupt models, and
  reuses a valid model without network access.

Default model location is `models/vosk-model-small-ru-0.22` **beside the Python
source**, independent of the process working directory. `VOSK_MODEL_PATH`
overrides it consistently for the installer, V6 and `vosk_api.py`.
`--destination` overrides the installer destination explicitly.
V6 validates file checksums before first model loading. Missing or damaged
models return HTTP 503 with setup instructions; there is no download during an
API request.

If the official host is unavailable, `VOSK_MODEL_URL` or `--url` can point to
an operator-controlled HTTPS mirror of **the identical pinned archive**. The
checksum remains mandatory; arbitrary mirrors and different model versions
are not silently accepted. No alternative public mirror was needed or claimed
as verified in this change.

## Render settings

Use the committed `Dockerfile` for predictable system dependencies. It installs
Python 3.12, Debian FFmpeg with H.264/AAC support and both Python requirements
files, downloads/verifies the model at image build time, then runs as a non-root
user. The model is part of the image: cold starts do not fetch it, and no paid
persistent disk is needed.

Settings for a separate test service (deployment is a manual user action):

| Setting | Value |
| --- | --- |
| Branch | `feature/smart-speech-v6` |
| Runtime | Docker |
| Dockerfile path | `./Dockerfile` |
| Docker build context | repository root |
| Instance type | Free |
| Health check path | `/health` |
| Docker command override | leave empty; use the image command |
| Automatic deployment | disable while comparing V5 and V6 |

The image uses Render's `PORT` (fallback `10000`) and one Uvicorn worker.
`VOSK_MODEL_PATH=/app/models/vosk-model-small-ru-0.22` is already configured in
the image. `OMP_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1` are also configured.
Do not point the model path at an empty ephemeral directory. No API keys or
other secrets are required. `/health` checks basic API/FFmpeg availability;
use a V6 diagnostic request to verify recognition readiness.

Build-time HTTPS destinations: Docker/Python base image registry, Debian
package mirrors, PyPI and `alphacephei.com`. For a private mirror use the Docker
build argument `VOSK_MODEL_URL`. Behind an organizational TLS proxy, the
Dockerfile accepts an optional BuildKit secret `ca_bundle` with the trusted CA
bundle; it is mounted only during the build and is not stored in the image.
No custom CA configuration is needed on ordinary Render networking.

For an existing native Python Render service, the build command is
`pip install -r requirements.txt -r requirements-vosk.txt && python download_vosk_model.py`,
and the start command is
`uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`.
That option requires **both ffmpeg and ffprobe** to already be installed on the
Render runtime; the Docker path provides them explicitly.

## Validation

Unit tests (no model download required):

```sh
python -m unittest discover -s tests -v
```

A live HTTP smoke test uses a **human Russian recording**, not TTS and not a
mock transcript. The source is the Golos corpus example at pinned commit
`1d7eac87f82dc2514d7c77450eb1b4017e6cb052`:
[recording and reference text](https://github.com/sberdevices/golos/tree/1d7eac87f82dc2514d7c77450eb1b4017e6cb052/examples/data).
The corpus [license](https://github.com/sberdevices/golos/tree/1d7eac87f82dc2514d7c77450eb1b4017e6cb052/license)
and attribution remain with its publishers. Audio is downloaded on explicit
smoke-test invocation and is not redistributed in this repository.

Start the API on port 8006, then run:

```sh
python tests/real_speech_smoke.py --output /tmp/v6-real-speech
```

Use `--base-url` for a different server. For a fully offline server/container,
provide `--audio /path/to/golos.wav`; the same pinned audio checksum is verified.
The test wraps the real speech in a generated vertical picture, posts it to
the actual V6 endpoint, compares recognition against the published reference,
and exports MP4s from both V5 and V6. It verifies word timing order, absence of
unwanted speech cuts, H.264/AAC, dimensions, actual decoded video frame count,
frame spacing and A/V stream durations. `diagnostics.json`, `result.json`,
`v5.mp4` and `v6.mp4` are saved to the requested output directory.

Results and resource measurements are recorded in `VALIDATION_V6.md`.

## Editing limits

V6 only recognizes adjacent exact word/phrase restarts with the existing
conservative score thresholds. ASR confidence is not a calibrated probability
that deleting speech is safe. Missing confidence, clear repeated speech and
ambiguous overlaps are preserved; at most five automatic cuts are applied.
Pauses shorter than 1.5 seconds are preserved, while long pauses retain 300 ms
at each edge. General detection of semantically bad takes remains limited.
Review the diagnostic timecodes before accepting an edit.

Video uses synchronized hard cuts and short audio fades. V6 explicitly retains
the rational frame rate for constant-frame-rate input; variable-frame-rate
input remains variable. This prevents the concat renderer from giving the last
CFR frame a zero duration. V5 retains its previous rendering behavior.

One short utterance is a real ASR smoke test, not an accuracy benchmark. The
picture in this fixture is generated, so it cannot establish visible lip sync
or acceptable cuts in actual talking-head footage. Those still need comparison
on user footage containing natural retakes and hesitations. Render Free can be
slow and may sleep; the existing 60-second/200-MB limits do not guarantee that
all high-resolution inputs fit its memory or request-time limits. Use short
clips and one request at a time for initial comparisons.
