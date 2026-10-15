


import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(
    title="Reels Studio Director API",
    version="0.4.1"
)

logger = logging.getLogger("reels_studio")

MAX_UPLOAD_SIZE = 200 * 1024 * 1024


@app.get("/health")
def health():
    return {
        "status": "ok",
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "mode": "low_memory",
        "smart_speech": "v2"
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
            output,
            folder,
            "trimmed.mp4"
        )

    except Exception:
        shutil.rmtree(
            folder, ignore_errors=True
        )
        raise

    finally:
        await file.close()


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
        output = folder / "smart_speech.mp4"

        # Detect silence in the original audio.
        detection = run_command([
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i", str(source),
            "-af",
            f"silencedetect=noise=-35dB:d={silence_threshold}",
            "-vn",
            "-f", "null",
            "-"
        ], timeout=90)

        # Read silence events in chronological order.
        events = re.findall(
            r"silence_(start|end):\s*([\d.]+)",
            detection.stderr
        )

        silences = []
        silence_start = None

        for kind, value in events:
            timestamp = float(value)

            if kind == "start":
                silence_start = timestamp

            elif silence_start is not None:
                if timestamp > silence_start:
                    silences.append(
                        (silence_start, timestamp)
                    )
                silence_start = None

        if silence_start is not None:
            silences.append(
                (silence_start, duration)
            )

        # Keep a natural pause of up to 0.30 s.
        # Remove only the excess silence.
        keep = []
        cursor = 0.0
        natural_pause = 0.30

        for a, b in silences:
            a = max(0.0, min(a, duration))
            b = max(a, min(b, duration))

            silence_length = b - a

            if silence_length <= natural_pause:
                continue

            # Preserve 0.15 s at each side.
            cut_start = a + natural_pause / 2
            cut_end = b - natural_pause / 2

            if cut_start > cursor + 0.04:
                keep.append(
                    (cursor, cut_start)
                )

            cursor = max(cursor, cut_end)

        if duration > cursor + 0.04:
            keep.append(
                (cursor, duration)
            )

        # Protect the free Render instance.
        if not keep or len(keep) > 12:
            keep = [(0.0, duration)]

        filters = []

        for i, (a, b) in enumerate(keep):
            segment_duration = b - a

            # Small audio fades reduce clicks
            # at the start and end of each segment.
            fade = min(
                0.025,
                segment_duration / 4
            )

            filters.append(
                f"[0:v]trim=start={a}:end={b},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )

            filters.append(
                f"[0:a]atrim=start={a}:end={b},"
                f"asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={fade:.4f},"
                f"afade=t=out:"
                f"st={segment_duration - fade:.4f}:"
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

        run_command(
            command,
            timeout=180
        )

        return send_video(
            output,
            folder,
            "smart_speech_v2.mp4"
        )

    except Exception:
        shutil.rmtree(
            folder, ignore_errors=True
        )
        raise

    finally:
        await file.close()
