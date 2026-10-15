
from pathlib import Path
from urllib.request import urlretrieve
from zipfile import ZipFile

MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = (
    "https://alphacephei.com/vosk/models/"
    + MODEL_NAME
    + ".zip"
)

destination = Path("models")
destination.mkdir(exist_ok=True)

model_dir = destination / MODEL_NAME

if model_dir.is_dir():
    print("Model already exists")
else:
    archive = destination / "model.zip"

    print("Downloading Russian Vosk model...")
    urlretrieve(MODEL_URL, archive)

    print("Extracting model...")
    with ZipFile(archive) as zip_file:
        zip_file.extractall(destination)

    archive.unlink()

    if not model_dir.is_dir():
        raise RuntimeError("Model extraction failed")

    print("Russian Vosk model is ready")
