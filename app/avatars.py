"""Profile pictures (docs/DESIGN.md §2, Accounts).

The web app crops a picture to a small square before it uploads it, so the
server only checks that the bytes are an image a browser shows safely. SVG is
refused because it can carry script. A picture lives in DATA_DIR/avatars,
named by its SHA-256, written once and never changed. Its name changes with
the picture, so a browser may keep it for good.
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .config import get_settings
from .models import User

NAME_RE = re.compile(r"^[0-9a-f]{64}\.(png|jpeg|gif|webp)$")

MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def image_format(data: bytes) -> str | None:
    """``png``, ``jpeg``, ``gif`` or ``webp`` by the first bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def avatars_dir() -> Path:
    return get_settings().data_dir / "avatars"


def path(name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    p = avatars_dir() / name
    return p if p.is_file() else None


def store(data: bytes, fmt: str) -> str:
    """Store a picture and return its name."""
    name = f"{hashlib.sha256(data).hexdigest()}.{fmt}"
    if path(name) is not None:
        return name
    folder = avatars_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".incoming-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, folder / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return name


def forget(db: Session, name: str | None) -> None:
    """Delete a picture that no account shows any more. Two accounts with the
    same picture share its file. Call it after the change is committed."""
    if not name or not NAME_RE.match(name):
        return
    if db.query(User.id).filter(User.avatar == name).first() is None:
        (avatars_dir() / name).unlink(missing_ok=True)
