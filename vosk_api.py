
import json
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from vosk import Model, KaldiRecognizer, SetLogLevel

app = FastAPI(
    title="Reels Vosk Speech Analyzer",
    version="0.1.0"
)

MODEL_PATH = Path("models/vosk-model-small-ru-0.22")
MAX_SIZE = 50 * 1024 * 1024

SetLogLevel(-1)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_exists": MODEL_PATH.is_dir(),
        "ffmpeg": shutil.which("ffmpeg") is not None
    }


@app.post("/analyze-speech")
async def analyze_speech(file: UploadFile = File(...)):
    folder = Path(tempfile.mkdtemp(prefix="vosk_"))

    try:
        source = folder / "source.mp4"
        audio_path = folder / "audio.wav"

        size = 0

        with source.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)

                if size > MAX_SIZE:
                    raise HTTPException(
                        413, "Maximum file size is 50 MB"
                    )

                output.write(chunk)

        if size == 0:
            raise HTTPException(400, "Empty file")

        if not MODEL_PATH.is_dir():
            raise HTTPException(503, "Vosk model not found")

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-threads", "1",
                    "-i", str(source),
                    "-vn",
                    "-ac", "1",
                    "-ar", "16000",
                    "-c:a", "pcm_s16le",
                    str(audio_path)
                ],
                check=True,
                capture_output=True,
                timeout=90
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Audio conversion timed out")
        except subprocess.CalledProcessError:
            raise HTTPException(422, "Cannot extract audio")

        model = Model(str(MODEL_PATH))
        recognizer = KaldiRecognizer(model, 16000)
        recognizer.SetWords(True)

        words = []

        with wave.open(str(audio_path), "rb") as audio:
            while True:
                chunk = audio.readframes(4000)

                if not chunk:
                    break

                if recognizer.AcceptWaveform(chunk):
                    result = json.loads(recognizer.Result())
                    words.extend(result.get("result", []))

            result = json.loads(recognizer.FinalResult())
            words.extend(result.get("result", []))

        return {
            "language": "ru",
            "text": " ".join(
                word["word"] for word in words
            ),
            "words": words,
            "word_count": len(words)
        }

    finally:
        await file.close()
        shutil.rmtree(folder, ignore_errors=True)
