"""Shared location and integrity checks for the pinned Russian Vosk model."""
import hashlib
import json
import os
from pathlib import Path

MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
# Fingerprint of the official HTTPS archive, verified during V6 setup.
MODEL_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
PROJECT_DIR = Path(__file__).resolve().parent
MODEL_FILES = json.loads((PROJECT_DIR / "vosk_model_files.json").read_text())


def model_path():
    configured = os.environ.get("VOSK_MODEL_PATH")
    return Path(configured).expanduser().resolve() if configured else PROJECT_DIR / "models" / MODEL_NAME


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_problem(path, *, verify_hashes=True):
    """Return a useful error, or None for a complete (optionally verified) model."""
    for relative, expected in MODEL_FILES.items():
        file = Path(path) / relative
        if file.is_symlink() or not file.is_file():
            return f"Missing model file: {relative}"
        if verify_hashes and file_sha256(file) != expected:
            return f"Model checksum mismatch: {relative}"
    return None
