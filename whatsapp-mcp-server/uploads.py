"""Temporary attachment uploads for remote MCP clients.

A remote client POSTs a file to /upload, gets an upload_id back, and passes it
to send_file / send_audio_message in place of a media_path. The file is
deleted once it has been sent; anything left over is swept after
UPLOAD_TTL_SECONDS.

UPLOAD_DIR must sit on the same volume (at the same path) that the bridge
mounts, because the bridge reads attachments by filesystem path. The bridge's
WHATSAPP_MEDIA_ROOTS must include UPLOAD_DIR; otherwise /api/send will refuse
the path we hand it.
"""

import os
import re
import secrets
import shutil
import time
from typing import Optional, Tuple

UPLOAD_DIR = os.environ.get(
    "UPLOAD_DIR",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "whatsapp-bridge",
        "store",
        "uploads",
    ),
)
UPLOAD_TTL_SECONDS = int(os.environ.get("UPLOAD_TTL_SECONDS", "3600"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024

# token_urlsafe(16) is always 22 chars from the url-safe alphabet.
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
# \w is Unicode-aware so names like 見本-太郎.pdf survive; only separators
# and symbols are replaced.
_UNSAFE = re.compile(r"[^\w. -]+")


def safe_filename(name: str) -> str:
    """Strip any path and odd characters, keeping the extension the bridge
    uses to pick the media type."""
    name = _UNSAFE.sub("_", os.path.basename(name.replace("\\", "/"))).strip(" ")
    if name.startswith(".") and name.count(".") == 1:
        name = "upload" + name  # a bare extension such as ".pdf"
    name = name.strip(".")
    if not name:
        return "upload.bin"
    stem, ext = os.path.splitext(name)
    ext = ext[:16]
    while len((stem + ext).encode()) > 200:
        stem = stem[:-1]
    return (stem or "upload") + ext


def new_upload(filename: str) -> Tuple[str, str]:
    """Create a fresh upload directory and return (upload_id, write path)."""
    upload_id = secrets.token_urlsafe(16)
    directory = os.path.join(UPLOAD_DIR, upload_id)
    os.makedirs(directory, mode=0o700)
    return upload_id, os.path.join(directory, safe_filename(filename))


def resolve(upload_id: str) -> Optional[str]:
    """Return the uploaded file's path, or None if the id is malformed,
    unknown or expired."""
    if not _UPLOAD_ID.match(upload_id or ""):
        return None
    directory = os.path.join(UPLOAD_DIR, upload_id)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    files = [n for n in names if os.path.isfile(os.path.join(directory, n))]
    return os.path.join(directory, files[0]) if len(files) == 1 else None


def scratch_dir() -> str:
    """A directory the bridge can also read, for files derived from an upload
    (e.g. converted audio)."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    return UPLOAD_DIR


def delete(upload_id: str) -> None:
    if _UPLOAD_ID.match(upload_id or ""):
        shutil.rmtree(os.path.join(UPLOAD_DIR, upload_id), ignore_errors=True)


def sweep() -> None:
    """Remove uploads (and stray converted files) older than the TTL."""
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    try:
        entries = list(os.scandir(UPLOAD_DIR))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
        except OSError:
            pass
