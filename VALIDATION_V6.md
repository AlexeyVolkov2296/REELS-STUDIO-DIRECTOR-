# Smart Speech V6 validation

## Model download

The official HTTPS URL returned 200 and delivered 46,236,750 bytes. SHA-256:
`961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4`.
The model was installed and loaded successfully. Running the installer again
from `/tmp` found and verified the same cached model beside the repository.
A separate Docker image build downloaded and verified the model again.

The earlier failure was an HTTPS proxy CONNECT 403. That policy denial was no
longer present during this validation; this change does not claim to bypass
network restrictions. The installer now reports this cause clearly and never
disables TLS/checksum verification. HTTPS mirrors are supported only for the
same pinned archive; no public mirror was substituted for the official source.

## Real Russian speech

Source: a 4.0-second recording of a human speaker from
[Golos](https://github.com/sberdevices/golos/tree/1d7eac87f82dc2514d7c77450eb1b4017e6cb052/examples/data),
`001ce26c07c20eaa0d666b824c6c6924.wav`.
Fixture SHA-256: `d066239503c4e7406ebeb47423334b5109aa6b30d62046d0338a04e41b4c52f5`.
No TTS, mocked recognition results or external speech API was used.

Published reference and local Vosk recognition both produced:

> шестьдесят тысяч тенге сколько будет стоить

Six reference words, zero substitutions, insertions or deletions on this one
sample. Timings were ordered and within the clip; the first word started at
0.45 s and the last ended at 3.57 s. V6 proposed **zero** deletions for this normal
utterance.

Both the existing `test_vosk.py` CLI and real HTTP requests to
`/smart-speech-v6?diagnostics_only=true` were executed. The live HTTP smoke test
also rendered `/smart-speech-v5` and `/smart-speech-v6` from the same input,
consisting of the human recording and a generated portrait picture.

| Export | Duration | Picture | Video/audio | Result |
| --- | --- | --- | --- | --- |
| V5 | 3.333 s | 180 × 320 | H.264/AAC | Existing pause-trimming behavior |
| V6 | 4.000 s | 180 × 320 | H.264/AAC | Normal utterance retained |

V6 output had 100 decoded frames spaced 40 ms apart (25 fps), with A/V stream
end times within one frame. The first real export test caught a zero-duration
last-frame problem in FFmpeg's concat output. V6 now sets the source rational
CFR explicitly; the complete frame-count check then passed. V5's rendering
settings were not changed.

## Docker / free-instance resource check

The committed Dockerfile built successfully with the real model included.
The cloud development proxy required its existing DNS mapping and trusted CA
bundle to be forwarded to the build; the CA was supplied as a temporary
BuildKit secret. TLS verification and Debian package signatures stayed enabled.
These proxy-specific settings are not embedded in the image.

The image was run as its non-root user with:

```sh
docker run -d --name reels-v6-check --network none \
  --memory 512m --memory-swap 512m --cpus 0.1 reels-v6-validation
```

The same smoke test ran **inside the container**, using the already downloaded
and checksum-verified audio fixture. The API had no outgoing network access.

- Cold V6 diagnostics including recognition: **15.596 s**.
- V5 export: **2.697 s**.
- V6 recognition plus export with the loaded model: **10.896 s**.
- Peak cgroup memory through this test: **289,247,232 bytes (~276 MiB)**.
- Memory cgroup `oom=0`, `oom_kill=0`; Docker `OOMKilled=false`.

The local host also passed the live API test with FFmpeg 7.1.5. The container
uses Debian FFmpeg 5.1.9; no model download or paid service is needed at runtime.
This is a local resource-limited container check, **not a deployed Render test**.

## Automated checks

- `python -m unittest discover -s tests -v`: **17 passed**.
- Existing nine editing-safety tests remained passing.
- Eight downloader/path tests cover both kinds of 403, transient retries,
  checksum rejection, HTTPS/downgrade protection, unsafe ZIP entries, cache
  verification, corrupt-model repair and shared model path selection.
- `pip check`: passed in the development environment and Docker image.
- `git diff --check`: passed.

Reproduce the live test with `tests/real_speech_smoke.py`; see
`SMART_SPEECH_V6.md` for commands and Render settings. Its outputs include both
MP4s, recognized words and timing/results JSON.

## Remaining limits

One clear utterance is not a recognition-quality benchmark and does not cover
natural failed takes, accents, noise or deliberate rhetorical repetition. The
fixture's picture is generated, so actual face continuity and visible lip sync
still require user footage. High-resolution or 60-second clips and concurrent
requests were not load-tested on Render Free. Actual Render deployment, cold
starts and provider request timeouts remain unverified. No deployment or merge
was performed.
