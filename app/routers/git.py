"""Cloning a paper's history with git (docs/DESIGN.md §9, Cloning).

``git clone https://meertex.com/p/<name>.git`` fetches every snapshot and the
version tags, for backups and exports. It is git's smart HTTP protocol, and
only ``git upload-pack`` ever runs, so the repository is read-only from
outside. A push is refused with a message that says where edits go instead.
The live document is the paper's source and git its record. A pushed commit
would have to be merged into text that coauthors are changing at the same
time, which is what ``meertex sync`` already does, file by file.

git asks for a user name and a password. The password is the account's API
token, the user name is not looked at. ``Authorization: Bearer <token>``
works too, for scripts. Without either the answer is 401, whether the paper
exists or not.
"""

import base64
import contextlib
import logging
import subprocess
import tempfile
import zlib
from typing import IO, Annotated

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import (
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy.orm import Session as DBSession

from .. import live
from ..access import resolve_project
from ..config import get_settings
from ..database import DB
from ..live import git
from ..models import Project
from ..naming import valid_name
from ..security import AuthorizationHeader, get_user_by_api_token

router = APIRouter(tags=["git"])
logger = logging.getLogger("uvicorn.error")
settings = get_settings()

# What an upload-pack request may grow to once unpacked. A request lists the
# refs the client wants and the commits it has, so even a large fetch stays
# far below this.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

PUSH_REFUSED = ("meertex does not take pushes. Edit in the browser, or in a checkout "
                "that meertex sync keeps in step (meertex checkout {name}).")

NO_CACHE = {"Cache-Control": "no-cache, max-age=0, must-revalidate", "Pragma": "no-cache"}

GitProtocol = Annotated[str | None, Header()]


def _pkt(line: str) -> bytes:
    """One line in git's pkt-line framing: four hex digits of length, then the line."""
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


def _text(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    # git prints a text/plain body line by line as "remote: ...".
    return PlainTextResponse(message + "\n", status_code=status, headers={**NO_CACHE, **(headers or {})})


def _token(authorization: str | None) -> str | None:
    """The API token of a Basic (the password) or Bearer Authorization header."""
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() == "bearer":
        return value.strip() or None
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded.partition(":")[2] or None
    return None


def _project(db: DBSession, authorization: str | None, repo: str) -> Project | Response:
    """The paper ``repo`` names (its name or id, with or without ``.git``), or
    the answer that refuses it."""
    user = get_user_by_api_token(db, _token(authorization))
    if user is None:
        return _text(401, "Sign in with your email as the user name and your API token as the password. "
                          f"The token is on {settings.base_url.rstrip('/')}/settings#token.",
                     {"WWW-Authenticate": 'Basic realm="meertex", charset="UTF-8"'})
    name = repo.removesuffix(".git")
    try:
        project, _membership = resolve_project(db, user, name, "viewer")
    except HTTPException as exc:
        if exc.status_code == 409:
            return _text(409, f"More than one of your papers is called {name}. "
                              "Use the paper's id instead of its name, as in /p/<id>.git.")
        return _text(404, "Repository not found.")
    return project


def _found(repo: str, db: DB, authorization: AuthorizationHeader = None) -> Project | Response:
    return _project(db, authorization, repo)


Found = Annotated[Project | Response, Depends(_found)]


@router.get("/p/{repo}.git", include_in_schema=False)
def repo_page(repo: str) -> RedirectResponse:
    """The clone address opened in a browser shows the paper."""
    return RedirectResponse(f"/p/{repo}", status_code=302)


@router.get("/p/{repo}/info/refs")
def info_refs(
    repo: str,
    db: DB,
    service: str = "",
    authorization: AuthorizationHeader = None,
    git_protocol: GitProtocol = None,
) -> Response:
    """The refs, which is how every clone, fetch and push begins."""
    if service == "git-receive-pack":
        # Before asking for a password, so a push fails at once and says why.
        # The name is the one in the address, so it is only repeated when it
        # could be a paper's name.
        name = repo.removesuffix(".git")
        message = PUSH_REFUSED.format(name=name if valid_name(name) else "<name>")
        return Response(_pkt(f"ERR {message}\n"), headers=NO_CACHE,
                        media_type="application/x-git-receive-pack-advertisement")
    if service != "git-upload-pack":
        return _text(403, "meertex serves git's smart HTTP protocol only. Please update git.")
    found = _project(db, authorization, repo)
    if isinstance(found, Response):
        return found
    try:
        live.snapshot_pending(found.id)
    except Exception:
        # The clone still gets every snapshot before this one.
        logger.exception("The snapshot before a clone of project %s failed", found.id)
    argv, env = git.upload_pack(found.id, git_protocol, advertise=True)
    proc = subprocess.run(argv, env=env, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0:
        logger.error("git upload-pack --advertise-refs failed for project %s: %s",
                     found.id, proc.stderr.decode(errors="replace").strip())
        return _text(500, "The repository could not be read.")
    body = proc.stdout
    if "version=2" not in env.get("GIT_PROTOCOL", "").split(":"):
        # Protocol v0 starts with the service line, v2 with its own "version 2".
        body = _pkt("# service=git-upload-pack\n") + b"0000" + body
    return Response(body, headers=NO_CACHE, media_type="application/x-git-upload-pack-advertisement")


async def _request_file(request: Request) -> IO[bytes] | Response:
    """The request body, unpacked when git sent it gzipped (it does above
    1 KB), in a temporary file that becomes upload-pack's stdin."""
    gzipped = request.headers.get("content-encoding", "").lower() in ("gzip", "x-gzip")
    unzip = zlib.decompressobj(wbits=31) if gzipped else None
    out = tempfile.TemporaryFile()  # noqa: SIM115  (handed on open, _serve closes it)
    size = 0
    try:
        async for chunk in request.stream():
            # One byte over the limit is enough to know it is too large.
            data = unzip.decompress(chunk, MAX_REQUEST_BYTES + 1 - size) if unzip else chunk
            size += len(data)
            if size > MAX_REQUEST_BYTES:
                out.close()
                return _text(413, "The request is too large.")
            out.write(data)
    except zlib.error:
        out.close()
        return _text(400, "The request body is not valid gzip.")
    out.seek(0)
    return out


async def _serve(argv: list[str], env: dict[str, str], stdin: IO[bytes], project_id: str):
    """upload-pack's stdout as it comes. upload-pack starts with the response,
    so a client that left before never starts one, and one that leaves during
    it stops it. stdin and stderr are files, not pipes, so upload-pack never
    blocks on one of them while this side waits for the other."""
    with stdin, tempfile.TemporaryFile() as stderr:
        proc = await anyio.open_process(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr, env=env)
        finished = False
        try:
            async for chunk in proc.stdout:
                yield chunk
            finished = True
        finally:
            with anyio.CancelScope(shield=True):
                if not finished:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                code = await proc.wait()
                await proc.aclose()
            if finished and code != 0:
                stderr.seek(0)
                logger.error("git upload-pack failed for project %s: %s",
                             project_id, stderr.read(4000).decode(errors="replace").strip())


@router.post("/p/{repo}/git-upload-pack")
async def upload_pack(found: Found, request: Request, git_protocol: GitProtocol = None) -> Response:
    """The objects the client asked for, as a pack."""
    if isinstance(found, Response):
        return found
    stdin = await _request_file(request)
    if isinstance(stdin, Response):
        return stdin
    argv, env = git.upload_pack(found.id, git_protocol)
    return StreamingResponse(_serve(argv, env, stdin, found.id), headers=NO_CACHE,
                             media_type="application/x-git-upload-pack-result")
