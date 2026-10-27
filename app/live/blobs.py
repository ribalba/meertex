"""Binary files of a project, stored by SHA-256 (docs/DESIGN.md §8).

The live document only names a blob by its hash. The bytes live in
DATA_DIR/projects/<id>/blobs/<sha>, written once and never changed, so a hash
that is there is always complete (written to a temporary name first).
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from ..config import get_settings

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def blobs_dir(project_id: str) -> Path:
    return get_settings().projects_dir / project_id / "blobs"


def path(project_id: str, sha: str) -> Path | None:
    if not SHA_RE.match(sha):
        return None
    p = blobs_dir(project_id) / sha
    return p if p.is_file() else None


def store(project_id: str, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    if path(project_id, sha) is not None:
        return sha
    folder = blobs_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".incoming-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, folder / sha)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return sha


def read(project_id: str, sha: str) -> bytes | None:
    p = path(project_id, sha)
    return p.read_bytes() if p is not None else None


def copy_all(src_id: str, dst_id: str, shas: set[str]) -> None:
    """Copy the named blobs from one project to another (a fork)."""
    for sha in shas:
        data = read(src_id, sha)
        if data is not None:
            store(dst_id, data)
