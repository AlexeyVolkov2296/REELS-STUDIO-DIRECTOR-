
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

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


def render_segments(source, output, keep):
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

