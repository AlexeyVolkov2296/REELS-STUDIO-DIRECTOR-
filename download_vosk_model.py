"""Install the pinned Russian Vosk model without accepting partial downloads."""
import argparse
import fcntl
import hashlib
import http.client
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from vosk_model import MODEL_FILES, MODEL_NAME, MODEL_SHA256, MODEL_URL, model_path, model_problem

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024


class ModelDownloadError(RuntimeError):
    pass


class HTTPSOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ModelDownloadError("Refusing a model download redirect without HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(request, timeout):
    # Use the system CA store and configured HTTPS proxy; never disable TLS.
    return build_opener(HTTPSOnlyRedirect()).open(request, timeout=timeout)


def download_archive(url, archive, *, attempts=3, timeout=30):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ModelDownloadError("VOSK_MODEL_URL must be an HTTPS URL without embedded credentials")
    request = Request(url, headers={"User-Agent": "Reels-Studio-Director/Vosk-model-installer"})
    for attempt in range(1, attempts + 1):
        try:
            digest = hashlib.sha256()
            size = 0
            with open_url(request, timeout) as response, archive.open("wb") as target:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ModelDownloadError("Model archive exceeds the size limit")
                    target.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != MODEL_SHA256:
                raise ModelDownloadError("Model archive SHA-256 mismatch; refusing to install")
            return
        except HTTPError as exc:
            if exc.code == 403:
                raise ModelDownloadError(
                    f"HTTP 403 for {parsed.hostname}. Allow this host in the HTTPS proxy/network "
                    "settings, or set VOSK_MODEL_URL to a trusted HTTPS mirror of the same archive. "
                    "The pinned SHA-256 is still required."
                ) from exc
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise ModelDownloadError(f"Model server returned HTTP {exc.code}") from exc
            failure = f"HTTP {exc.code}"
        except URLError as exc:
            # CONNECT proxy denials are URLError rather than HTTPError.
            if "403" in str(exc.reason):
                raise ModelDownloadError(
                    f"HTTPS proxy returned 403 for {parsed.hostname}; allow that hostname in "
                    "network settings. Changing TLS verification cannot fix a proxy denial."
                ) from exc
            failure = type(exc.reason).__name__
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            failure = type(exc).__name__
        if attempt == attempts:
            raise ModelDownloadError(f"Model download failed after {attempts} attempts ({failure})")
        print(f"Temporary download failure ({failure}); retry {attempt + 1}/{attempts}", flush=True)
        time.sleep(min(2 ** (attempt - 1), 4))


def extract_archive(archive, staging):
    with ZipFile(archive) as source:
        files = [member for member in source.infolist() if not member.is_dir()]
        expected = {f"{MODEL_NAME}/{name}" for name in MODEL_FILES}
        if {member.filename for member in files} != expected or len(files) != len(expected):
            raise ModelDownloadError("Unexpected files or layout in the model archive")
        if sum(member.file_size for member in files) > MAX_EXTRACTED_BYTES:
            raise ModelDownloadError("Extracted model exceeds the size limit")
        for member in files:
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ModelDownloadError("Symlinks are not allowed in the model archive")
            # Names must exactly match the pinned manifest; no extractall/path traversal.
            target = staging / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    candidate = staging / MODEL_NAME
    problem = model_problem(candidate)
    if problem:
        raise ModelDownloadError(problem)
    return candidate


def install_model(destination=None, url=None, *, attempts=3, timeout=30):
    if not 1 <= attempts <= 5 or not 1 <= timeout <= 120:
        raise ValueError("attempts must be 1–5 and timeout must be 1–120 seconds")
    destination = Path(destination or model_path()).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = url or os.environ.get("VOSK_MODEL_URL") or MODEL_URL
    with (destination.parent / f".{destination.name}.install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if model_problem(destination) is None:
            print(f"Verified cached model: {destination}", flush=True)
            return destination
        with tempfile.TemporaryDirectory(prefix=".vosk-install-", dir=destination.parent) as temporary:
            staging = Path(temporary)
            archive = staging / "model.zip"
            print(f"Downloading {MODEL_NAME} from {urlsplit(url).hostname}", flush=True)
            download_archive(url, archive, attempts=attempts, timeout=timeout)
            candidate = extract_archive(archive, staging)
            backup = staging / "previous-model"
            if destination.exists():
                destination.rename(backup)
            try:
                candidate.rename(destination)
            except OSError:
                if backup.exists():
                    backup.rename(destination)
                raise
        print(f"Verified Russian Vosk model installed: {destination}", flush=True)
        return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, help="Overrides VOSK_MODEL_PATH")
    parser.add_argument("--url", help="HTTPS mirror; must match the pinned official SHA-256")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        install_model(args.destination, args.url, attempts=args.attempts, timeout=args.timeout)
    except (ModelDownloadError, OSError, ValueError) as exc:
        parser.exit(1, f"Model installation failed: {exc}\n")


if __name__ == "__main__":
    main()
