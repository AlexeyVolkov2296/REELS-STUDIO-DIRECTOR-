# Smart Speech V6

V6 is an additional `POST /smart-speech-v6` endpoint. V5 and its behavior are
unchanged. V6 uses local Russian Vosk word timings and FFmpeg silence detection;
there is no paid API call. The optional `?diagnostics_only=true` response is
JSON with the transcript, proposed deletion timecodes, reasons, confidence,
and whether each proposal would be applied. Normal MP4 responses include the
proposals in `X-Smart-Speech-Report`.

## Local setup

Install both dependency sets and install the Russian model outside git:

```sh
pip install -r requirements.txt -r requirements-vosk.txt
python download_vosk_model.py
```

The model is read from `models/vosk-model-small-ru-0.22` relative to the
process working directory, or from `VOSK_MODEL_PATH`. V6 returns HTTP 503 with
a clear message when either the model or optional Vosk runtime is missing;
other endpoints remain available.

## Render free instance

The repository has no Render Blueprint. Configure the existing web service
with a Python 3.12 runtime, build command
`pip install -r requirements.txt -r requirements-vosk.txt && python download_vosk_model.py`,
and start command `uvicorn app:app --host 0.0.0.0 --port $PORT`. The model is
downloaded at build time and occupies the deployed app filesystem; no persistent
disk is needed. Model downloads need outbound HTTPS access to `alphacephei.com`.
The free instance has limited CPU and memory, so V6 processes one upload at a
time per worker; avoid multiple workers and large concurrent uploads. Recognition
on Render still needs a real deployment smoke test before relying on it.

## Editing safeguards

V6 proposes a word stutter only when Vosk confidence is low for the first word,
high for its adjacent retry, and the retry is materially more confident. Exact
2–6 word restarts follow the same conservative confidence rule and must be
within 1.5 seconds. Repeated speech with missing/equal confidence is preserved.
Silences shorter than 1.5 seconds are preserved; a long pause proposal removes
only its center and retains 300 ms at both edges. Ambiguous overlapping cuts
are reported and not applied. Review the timecodes before using an edited file.

Video uses synchronized hard cuts, retaining the input frame rate and frame
sequence without video crossfades (which can create face ghosting). Audio is
joined with short fades by the existing FFmpeg segment renderer. Output is MP4,
H.264 and AAC, matching V5's export settings.

Run detection tests with `python -m unittest discover -s tests -v`.
