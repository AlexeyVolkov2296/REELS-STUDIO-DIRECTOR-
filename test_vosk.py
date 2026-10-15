
import json
import sys
import wave
from pathlib import Path


def transcribe(model_path, wav_path):
    from vosk import Model, KaldiRecognizer, SetLogLevel

    SetLogLevel(-1)

    model = Model(model_path)
    recognizer = KaldiRecognizer(model, 16000)
    recognizer.SetWords(True)

    words = []

    with wave.open(wav_path, "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getframerate() != 16000
        ):
            raise ValueError(
                "Audio must be mono, 16-bit PCM, 16000 Hz"
            )

        while True:
            chunk = audio.readframes(4000)

            if not chunk:
                break

            if recognizer.AcceptWaveform(chunk):
                result = json.loads(recognizer.Result())
                words.extend(result.get("result", []))

        final_result = json.loads(recognizer.FinalResult())
        words.extend(final_result.get("result", []))

    return words


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(
            "Usage: python test_vosk.py MODEL_DIR AUDIO_WAV"
        )

    model_path = sys.argv[1]
    wav_path = sys.argv[2]

    if not Path(model_path).is_dir():
        sys.exit("Model directory not found")

    result = transcribe(model_path, wav_path)

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        )
    )

