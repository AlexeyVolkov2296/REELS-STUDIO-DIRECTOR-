import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

app = FastAPI(title="Reels Studio Director API", version="0.1.0")

@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg": bool(__import__('shutil').which('ffmpeg'))}

@app.post("/trim")
async def trim_video(file: UploadFile = File(...), start: float = 0, duration: float = 8):
    if start < 0 or duration <= 0 or duration > 120:
        raise HTTPException(400, "Invalid start or duration")
    if not __import__('shutil').which('ffmpeg'):
        raise HTTPException(503, "FFmpeg is not installed on server")
    workdir = Path(tempfile.mkdtemp(prefix="reels_"))
    source = workdir / "source.mp4"
    output = workdir / "trimmed.mp4"
    try:
        with source.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                target.write(chunk)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(source), "-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(output)]
        subprocess.run(cmd, check=True, timeout=180, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(422, f"Video processing failed: {str(exc)[:150]}")
    return FileResponse(output, media_type="video/mp4", filename="trimmed.mp4")
