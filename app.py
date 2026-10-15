
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(
    title="Reels Studio Director API",
    version="0.3.0"
)

logger = logging.getLogger("reels_studio")

MAX_UPLOAD_SIZE = 200 * 1024 * 1024


@app.get("/health")
def health():
    return {
        "status": "ok",
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "mode": "low_memory"
    }


@app.post("/trim")
async def trim_video(
    file: UploadFile = File(...),
    start: float = 0,
    duration: float = 8
):
    if start < 0 or duration <= 0 or duration > 120:
        raise HTTPException(
            status_code=400,
            detail="Invalid start or duration"
        )

    if not shutil.which("ffmpeg"):
        raise HTTPException(
            status_code=503,
            detail="FFmpeg is not installed"
        )

    workdir = Path(tempfile.mkdtemp(prefix="reels_"))

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mov", ".mp4", ".m4v", ".mkv", ".webm"}:
        suffix = ".mp4"

    source = workdir / ("source" + suffix)
    output = workdir / "trimmed.mp4"

    try:
        total_size = 0

        with source.open("wb") as target:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="File exceeds 200 MB limit"
                    )

                target.write(chunk)

        if total_size == 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty"
            )

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

            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-threads:v", "1",
            "-crf", "28",
            "-pix_fmt", "yuv420p",

            "-c:a", "aac",
            "-b:a", "96k",

            "-movflags", "+faststart",

            str(output)
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=180,
                check=False
            )

        except subprocess.TimeoutExpired:
            logger.error("FFmpeg timed out")

            raise HTTPException(
                status_code=504,
                detail="Video processing timed out"
            )

        if result.returncode != 0:
            error_message = (
                result.stderr
                or result.stdout
                or "Unknown FFmpeg error"
            ).strip()

            logger.error(
                "FFmpeg failed: %s",
                error_message[-4000:]
            )

            raise HTTPException(
                status_code=422,
                detail="FFmpeg error: " + error_message[-1200:]
            )

        if not output.exists() or output.stat().st_size == 0:
            raise HTTPException(
                status_code=422,
                detail="FFmpeg did not produce an output file"
            )

        return FileResponse(
            path=output,
            media_type="video/mp4",
            filename="trimmed.mp4",
            background=BackgroundTask(
                shutil.rmtree,
                workdir,
                ignore_errors=True
            )
        )

    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    except Exception:
        logger.exception("Unexpected processing error")

        shutil.rmtree(workdir, ignore_errors=True)

        raise HTTPException(
            status_code=500,
            detail="Unexpected video processing error"
        )

    finally:
        await file.close()
