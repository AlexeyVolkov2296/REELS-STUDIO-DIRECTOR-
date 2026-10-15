
import logging
import json
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from fractions import Fraction

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from vosk_model import model_path, model_problem

app = FastAPI(
    title="Reels Studio Director API",
    version="0.6.0"
)

logger = logging.getLogger("reels_studio")
MAX_UPLOAD_SIZE = 200 * 1024 * 1024

VOSK_URL = (
    "https://reels-vosk-test.onrender.com/analyze-speech"
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.6.0",
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "mode": "low_memory",
        "smart_speech": "v5"
    }


async def save_upload(file, folder):
    suffix = Path(file.filename or "").suffix.lower()

    if suffix not in {
        ".mov", ".mp4", ".m4v", ".mkv", ".webm"
    }:
        suffix = ".mp4"

    source = folder / ("source" + suffix)
    total = 0

    with source.open("wb") as target:
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            total += len(chunk)

            if total > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    413, "File exceeds 200 MB"
                )

            target.write(chunk)

    if total == 0:
        raise HTTPException(400, "Empty file")

    return source


def run_command(command, timeout=180):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            504, "Processing timed out"
        )

    if result.returncode != 0:
        logger.error(
            "Command failed: %s",
            result.stderr[-3000:]
        )
        raise HTTPException(
            422,
            "Processing failed: " + result.stderr[-800:]
        )

    return result


def video_info(source):
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source)
    ], timeout=30)

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        raise HTTPException(
            422, "Cannot read video duration"
        )

    if not 0 < duration <= 60:
        raise HTTPException(
            400,
            "Smart Speech accepts videos up to 60 seconds"
        )

    return duration


def encode_settings():
    return [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads:v", "1",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart"
    ]


def send_video(output, folder, filename):
    if (
        not output.exists()
        or output.stat().st_size == 0
    ):
        raise HTTPException(
            422, "Output video was not created"
        )

    return FileResponse(
        output,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(
            shutil.rmtree,
            folder,
            ignore_errors=True
        )
    )


def render_segments(source, output, keep, *, video_options=()):
    if not keep or len(keep) > 12:
        raise HTTPException(
            422, "Too many edits for this free instance"
        )

    filters = []

    for i, (a, b) in enumerate(keep):
        length = b - a
        fade = min(0.025, length / 4)

        filters.append(
            f"[0:v]trim=start={a:.5f}:end={b:.5f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )

        filters.append(
            f"[0:a]atrim=start={a:.5f}:end={b:.5f},"
            f"asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade:.4f},"
            f"afade=t=out:"
            f"st={length-fade:.4f}:"
            f"d={fade:.4f}[a{i}]"
        )

    streams = "".join(
        f"[v{i}][a{i}]"
        for i in range(len(keep))
    )

    filters.append(
        f"{streams}"
        f"concat=n={len(keep)}:"
        "v=1:a=1[vout][aout]"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-threads", "1",
        "-filter_threads", "1",
        "-i", str(source),
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-map", "[aout]",
        *encode_settings(),
        *video_options,
        str(output)
    ]

    run_command(command, timeout=180)


@app.post("/trim")
async def trim_video(
    file: UploadFile = File(...),
    start: float = 0,
    duration: float = 8
):
    if start < 0 or not 0 < duration <= 120:
        raise HTTPException(
            400, "Invalid start or duration"
        )

    folder = Path(
        tempfile.mkdtemp(prefix="reels_trim_")
    )

    try:
        source = await save_upload(file, folder)
        output = folder / "trimmed.mp4"

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-threads", "1",
            "-filter_threads", "1",
            "-ss", str(start),
            "-i", str(source),
            "-t", str(duration),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf",
            "scale=720:720:"
            "force_original_aspect_ratio=decrease:"
            "force_divisible_by=2",
            *encode_settings(),
            str(output)
        ]

        run_command(command)

        return send_video(
            output, folder, "trimmed.mp4"
        )

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    finally:
        await file.close()


def find_silences(source, duration, threshold):
    result = run_command([
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(source),
        "-af",
        f"silencedetect=noise=-35dB:d={threshold}",
        "-vn",
        "-f", "null",
        "-"
    ], timeout=90)

    events = re.findall(
        r"silence_(start|end):\s*([\d.]+)",
        result.stderr
    )

    silences = []
    start = None

    for kind, value in events:
        timestamp = float(value)

        if kind == "start":
            start = timestamp

        elif start is not None:
            a = max(0.0, min(start, duration))
            b = max(a, min(timestamp, duration))

            if b > a:
                silences.append((a, b))

            start = None

    if start is not None:
        a = max(0.0, min(start, duration))
        if duration > a:
            silences.append((a, duration))

    return silences


def keep_from_silences(
    silences,
    duration,
    natural_pause,
    max_cuts=10
):
    cuts = []

    for a, b in silences:
        if b - a <= natural_pause + 0.08:
            continue

        cut_start = a + natural_pause / 2
        cut_end = b - natural_pause / 2

        if cut_end - cut_start > 0.08:
            cuts.append((cut_start, cut_end))

    # Limit cuts without discarding the rest of the video.
    cuts = cuts[:max_cuts]

    keep = []
    cursor = 0.0

    for a, b in cuts:
        if a > cursor + 0.04:
            keep.append((cursor, a))
        cursor = max(cursor, b)

    if duration > cursor + 0.04:
        keep.append((cursor, duration))

    return keep or [(0.0, duration)]


@app.post("/smart-speech")
async def smart_speech(
    file: UploadFile = File(...),
    silence_threshold: float = 0.6
):
    if not 0.3 <= silence_threshold <= 2.0:
        raise HTTPException(
            400,
            "silence_threshold must be between 0.3 and 2.0"
        )

    folder = Path(
        tempfile.mkdtemp(prefix="reels_speech_")
    )

    try:
        source = await save_upload(file, folder)
        duration = video_info(source)

        silences = find_silences(
            source, duration, silence_threshold
        )

        keep = keep_from_silences(
            silences, duration, 0.30
        )

        output = folder / "smart_speech.mp4"

        render_segments(source, output, keep)

        return send_video(
            output, folder, "smart_speech_v2.mp4"
        )

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    finally:
        await file.close()


def vosk_words(source):
    try:
        with source.open("rb") as stream:
            response = requests.post(
                VOSK_URL,
                files={
                    "file": (
                        source.name,
                        stream,
                        "application/octet-stream"
                    )
                },
                timeout=(25, 240)
            )

        response.raise_for_status()
        data = response.json()

    except (
        requests.RequestException,
        ValueError
    ) as exc:
        logger.warning("Vosk unavailable: %s", exc)
        raise HTTPException(
            502,
            "Vosk service unavailable or timed out"
        )

    words = data.get("words")

    if not isinstance(words, list):
        raise HTTPException(
            502,
            "Vosk response has no word timings"
        )

    return words


def repeated_word_cuts(words, duration):
    cuts = []

    for prev, curr in zip(words, words[1:]):
        try:
            first = str(
                prev["word"]
            ).lower().strip(".,!?;: ")

            second = str(
                curr["word"]
            ).lower().strip(".,!?;: ")

            a = float(prev["start"])
            b = float(prev["end"])
            c = float(curr["start"])

        except (KeyError, TypeError, ValueError):
            continue

        if (
            first
            and first == second
            and len(first) >= 3
            and 0.08 <= b - a <= 0.7
            and 0 <= c - b <= 0.20
            and 0 <= a < b <= duration
        ):
            cut_a = max(0.0, a + 0.02)
            cut_b = min(duration, b - 0.02)

            if cut_b - cut_a >= 0.08:
                cuts.append((cut_a, cut_b))

    cuts.sort()
    nonoverlap = []

    for a, b in cuts:
        if (
            not nonoverlap
            or a >= nonoverlap[-1][1]
        ):
            nonoverlap.append((a, b))

    return nonoverlap[:8]


def keep_from_cuts(cuts, duration):
    keep = []
    cursor = 0.0

    for a, b in cuts:
        if a > cursor + 0.04:
            keep.append((cursor, a))
        cursor = max(cursor, b)

    if duration > cursor + 0.04:
        keep.append((cursor, duration))

    return keep or [(0.0, duration)]


@app.post("/smart-speech-v3")
async def smart_speech_v3(
    file: UploadFile = File(...)
):
    folder = Path(
        tempfile.mkdtemp(prefix="reels_v3_")
    )

    try:
        source = await save_upload(file, folder)
        duration = video_info(source)
        words = vosk_words(source)

        cuts = repeated_word_cuts(
            words, duration
        )

        keep = keep_from_cuts(
            cuts, duration
        )

        output = folder / "smart_speech_v3.mp4"

        render_segments(source, output, keep)

        return send_video(
            output, folder, "smart_speech_v3.mp4"
        )

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    finally:
        await file.close()


# ==========================================
# SMART SPEECH V5 — AUTOMATIC SMOOTH EDITING
# ==========================================

def render_smooth_segments(source, output, keep):
    if not keep or len(keep) > 11:
        raise HTTPException(
            422, "Too many segments"
        )

    if len(keep) == 1:
        render_segments(source, output, keep)
        return

    transition = 0.06
    lengths = [b - a for a, b in keep]

    if any(
        length <= transition * 3
        for length in lengths
    ):
        render_segments(source, output, keep)
        return

    filters = []

    for i, (a, b) in enumerate(keep):
        filters.append(
            f"[0:v]trim=start={a:.5f}:end={b:.5f},"
            f"setpts=PTS-STARTPTS,"
            f"fps=30,format=yuv420p,"
            f"setsar=1[v{i}]"
        )

        filters.append(
            f"[0:a]atrim=start={a:.5f}:end={b:.5f},"
            f"asetpts=PTS-STARTPTS,"
            f"aresample=48000,"
            f"aformat=sample_fmts=fltp:"
            f"sample_rates=48000:"
            f"channel_layouts=stereo[a{i}]"
        )

    current_v = "v0"
    current_a = "a0"
    accumulated = lengths[0]

    for i in range(1, len(keep)):
        next_v = f"vx{i}"
        next_a = f"ax{i}"

        offset = accumulated - transition

        filters.append(
            f"[{current_v}][v{i}]"
            f"xfade=transition=fade:"
            f"duration={transition}:"
            f"offset={offset:.5f}"
            f"[{next_v}]"
        )

        filters.append(
            f"[{current_a}][a{i}]"
            f"acrossfade=d={transition}:"
            f"c1=tri:c2=tri"
            f"[{next_a}]"
        )

        current_v = next_v
        current_a = next_a
        accumulated += lengths[i] - transition

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-threads", "1",
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-i", str(source),
        "-filter_complex", ";".join(filters),
        "-map", f"[{current_v}]",
        "-map", f"[{current_a}]",
        *encode_settings(),
        str(output)
    ]

    run_command(command, timeout=180)


# SMART SPEECH V6 uses local Vosk word timings and only removes highly
# supported retries. Model loading is lazy so V5 and the other endpoints
# continue to work when the optional model is not installed.
_v6_model = None
_v6_model_path = None


def v6_model_path():
    return model_path()


def transcribe_v6(source, folder):
    global _v6_model, _v6_model_path
    model_path = v6_model_path()
    if not model_path.is_dir():
        raise HTTPException(
            503,
            "Local Russian Vosk model is missing; set VOSK_MODEL_PATH",
        )
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError as exc:
        raise HTTPException(
            503,
            "Install the optional Vosk runtime from requirements-vosk.txt",
        ) from exc

    audio_path = folder / "v6_audio.wav"
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-threads", "1", "-i", str(source), "-vn", "-ac", "1",
        "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path),
    ], timeout=90)
    try:
        if _v6_model is None or _v6_model_path != model_path:
            problem = model_problem(model_path)
            if problem:
                raise HTTPException(503, problem + "; run download_vosk_model.py")
            SetLogLevel(-1)
            _v6_model = Model(str(model_path))
            _v6_model_path = model_path
        recognizer = KaldiRecognizer(_v6_model, 16000)
        recognizer.SetWords(True)
        words = []
        with wave.open(str(audio_path), "rb") as audio:
            while True:
                chunk = audio.readframes(4000)
                if not chunk:
                    break
                if recognizer.AcceptWaveform(chunk):
                    words.extend(json.loads(recognizer.Result()).get("result", []))
            words.extend(json.loads(recognizer.FinalResult()).get("result", []))
        return words
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Local Vosk transcription failed")
        raise HTTPException(502, "Local Russian speech recognition failed") from exc


def _v6_token(word):
    return str(word.get("word", "")).lower().strip(".,!?;:…—-()[]{}\"'«»")


def _v6_confidence(word):
    if "conf" not in word:
        return None
    try:
        value = float(word.get("conf", 0.0))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, value))


def _v6_safe_interval(words, start_index, end_index, reason, confidence):
    """Return a deletion proposal only when its audio evidence is strong."""
    first, last = words[start_index], words[end_index]
    try:
        start, end = float(first["start"]), float(last["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= start < end and end - start <= 4.0):
        return None
    return {
        "start": round(start, 3), "end": round(end, 3),
        "reason": reason, "confidence": round(confidence, 3),
    }


def detect_v6_retries(words, duration):
    """Detect only adjacent stutters and short exact phrase restarts.

    A cut is accepted only when the earlier take has low ASR confidence and
    its retry is materially clearer. Equal-confidence repetitions are kept.
    """
    proposals = []
    normalized = [_v6_token(word) for word in words]
    count = len(words)
    i = 0
    while i < count:
        token = normalized[i]
        # Single-word stutter; leave fillers and short grammatical words alone.
        if token and len(token) >= 3 and i + 1 < count and token == normalized[i + 1]:
            first, retry = words[i], words[i + 1]
            try:
                gap = float(retry["start"]) - float(first["end"])
            except (KeyError, TypeError, ValueError):
                gap = 99
            c1, c2 = _v6_confidence(first), _v6_confidence(retry)
            if (c1 is not None and c2 is not None and 0 <= gap <= 0.35
                    and c1 <= 0.55 and c2 >= 0.75 and c2 - c1 >= 0.25):
                proposal = _v6_safe_interval(
                    words, i, i, "low-confidence stutter corrected by clearer retry", c2 - c1
                )
                if proposal:
                    proposals.append(proposal)
                    i += 2
                    continue

        # Repeated 2–6 word phrase with a brief restart. Keep the clearer take.
        matched = False
        for size in range(min(6, (count - i) // 2), 1, -1):
            j = i + size
            if normalized[i:i + size] != normalized[j:j + size] or not all(normalized[i:i + size]):
                continue
            try:
                gap = float(words[j]["start"]) - float(words[i + size - 1]["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= gap <= 1.5:
                continue
            first_values = [_v6_confidence(w) for w in words[i:i + size]]
            retry_values = [_v6_confidence(w) for w in words[j:j + size]]
            if any(value is None for value in first_values + retry_values):
                continue
            first_conf = sum(first_values) / size
            retry_conf = sum(retry_values) / size
            if first_conf <= 0.50 and retry_conf >= 0.75 and retry_conf - first_conf >= 0.30:
                proposal = _v6_safe_interval(
                    words, i, i + size - 1,
                    "low-confidence repeated phrase followed by a clearer take",
                    retry_conf - first_conf,
                )
                if proposal:
                    proposals.append(proposal)
                    i += size * 2
                    matched = True
                    break
        if not matched:
            i += 1

    proposals.sort(key=lambda item: item["start"])
    safe = []
    for proposal in proposals:
        if proposal["end"] <= duration and (not safe or proposal["start"] >= safe[-1]["end"]):
            safe.append(proposal)
    return safe


def detect_v6_long_pauses(silences, duration, min_pause=1.5, padding=0.30):
    """Trim only the quiet center of long pauses, retaining audible padding."""
    proposals = []
    for start, end in silences:
        start, end = max(0.0, float(start)), min(duration, float(end))
        if end - start < min_pause:
            continue
        cut_start, cut_end = start + padding, end - padding
        if cut_end - cut_start >= 0.5:
            proposals.append({
                "start": round(cut_start, 3), "end": round(cut_end, 3),
                "reason": "center of long detected silence; 300 ms retained at each edge",
                "confidence": 0.90,
            })
    return proposals


def build_v6_report(words, silences, duration):
    proposals = detect_v6_retries(words, duration)
    proposals.extend(detect_v6_long_pauses(silences, duration))
    proposals.sort(key=lambda item: item["start"])
    # Overlapping proposals are ambiguous: report them but do not auto-cut.
    selected = []
    for proposal in proposals:
        if selected and proposal["start"] < selected[-1]["end"]:
            proposal["automatic"] = False
            selected[-1]["automatic"] = False
        elif sum(item["automatic"] for item in selected) >= 5:
            # Keep reports useful without producing an unrenderable edit list.
            proposal["automatic"] = False
        else:
            proposal["automatic"] = True
        selected.append(proposal)
    for proposal in selected:
        proposal.setdefault("automatic", True)
    return {
        "version": "v6", "duration": round(duration, 3),
        "language": "ru", "words": words,
        "proposed_deletions": selected,
        "note": "Only high-confidence non-overlapping proposals are applied; review all timecodes.",
    }


def v6_keep_segments(report):
    duration = report["duration"]
    cuts = [(item["start"], item["end"]) for item in report["proposed_deletions"] if item["automatic"]]
    return keep_from_cuts(cuts, duration)


def v6_video_options(source):
    result = run_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate", "-of", "json", str(source),
    ], timeout=30)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise HTTPException(422, "Video stream not found")
    try:
        nominal = Fraction(streams[0]["r_frame_rate"])
        average = Fraction(streams[0]["avg_frame_rate"])
        if nominal > 0 and nominal == average:
            # concat has a microsecond time base and can otherwise mux the last
            # frame with zero duration. Retain the source rational CFR explicitly.
            return ["-r", str(nominal), "-fps_mode", "cfr"]
    except (KeyError, ValueError, ZeroDivisionError):
        pass
    return ["-fps_mode", "vfr"]


@app.post("/smart-speech-v6")
async def smart_speech_v6(
    file: UploadFile = File(...),
    diagnostics_only: bool = False,
    silence_threshold: float = 0.45,
):
    if not 0.3 <= silence_threshold <= 2.0:
        raise HTTPException(400, "silence_threshold must be between 0.3 and 2.0")
    folder = Path(tempfile.mkdtemp(prefix="reels_v6_"))
    try:
        source = await save_upload(file, folder)
        duration = video_info(source)
        words = transcribe_v6(source, folder)
        silences = find_silences(source, duration, silence_threshold)
        report = build_v6_report(words, silences, duration)
        if diagnostics_only:
            shutil.rmtree(folder, ignore_errors=True)
            return JSONResponse(report)
        keep = v6_keep_segments(report)
        output = folder / "smart_speech_v6.mp4"
        # Synchronized hard cuts avoid xfade face ghosting and preserve source FPS.
        render_segments(source, output, keep, video_options=v6_video_options(source))
        response = send_video(output, folder, "smart_speech_v6.mp4")
        response.headers["X-Smart-Speech-Report"] = json.dumps(
            {"proposed_deletions": report["proposed_deletions"]}, ensure_ascii=False
        )
        return response
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.post("/smart-speech-v5")
async def smart_speech_v5(
    file: UploadFile = File(...),
    silence_threshold: float = 0.45,
    natural_pause: float = 0.20
):
    if not 0.3 <= silence_threshold <= 2.0:
        raise HTTPException(
            400,
            "silence_threshold must be between 0.3 and 2.0"
        )

    if not 0.12 <= natural_pause <= 0.5:
        raise HTTPException(
            400,
            "natural_pause must be between 0.12 and 0.5"
        )

    folder = Path(
        tempfile.mkdtemp(prefix="reels_v5_")
    )

    try:
        source = await save_upload(file, folder)
        duration = video_info(source)

        silences = find_silences(
            source,
            duration,
            silence_threshold
        )

        keep = keep_from_silences(
            silences,
            duration,
            natural_pause,
            max_cuts=5
        )

        output = folder / "smart_speech_v5.mp4"

        render_smooth_segments(
            source, output, keep
        )

        return send_video(
            output,
            folder,
            "smart_speech_v5.mp4"
        )

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    finally:
        await file.close()

