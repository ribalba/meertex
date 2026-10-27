"""meertex-server: the web app, its API, and the live documents' sync endpoint.

One process with one worker: the live documents are held in memory as rooms
(app/live), so a second worker would hold a second, diverging copy.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .config import get_settings
from .database import init_db
from .routers import (
    auth,
    blobs,
    compiles,
    dates,
    files,
    folders,
    history,
    imports,
    members,
    projects,
    questions,
    references,
    reminders,
    reviews,
    sync,
    timeline,
    versions,
)
from .security import OptionalUser

settings = get_settings()
logger = logging.getLogger("uvicorn.error")
STATIC_DIR = Path(__file__).resolve().parent / "static"
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # The live slice starts its background work (snapshots, idle rooms) here
    # and writes every open room out on shutdown. The reminder mails (§5) run
    # beside it unless REMINDERS_ENABLED is off.
    from . import reminders as reminder_mails
    from .live import rooms

    async with rooms.running(), reminder_mails.running():
        yield


app = FastAPI(title="meertex", version=VERSION, lifespan=lifespan)

# Coolify's Traefik terminates TLS. Trust its X-Forwarded-* so request.url and
# the client address are the real ones.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
# The editor's bundle is 430 KB and 140 KB compressed. Traefik passes
# responses through as they are, so the app compresses them itself.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

app.include_router(auth.router)
app.include_router(folders.router)
app.include_router(projects.router)
app.include_router(dates.router)
app.include_router(members.router)
app.include_router(versions.router)
app.include_router(questions.router)
app.include_router(files.router)
app.include_router(imports.router)
app.include_router(blobs.router)
app.include_router(history.router)
app.include_router(sync.router)
app.include_router(compiles.router)
app.include_router(reviews.router)
app.include_router(timeline.router)
app.include_router(reminders.router)
app.include_router(references.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/version")
def version() -> dict:
    return {"version": VERSION}


class RevalidatingStatic(StaticFiles):
    """Static files the browser must revalidate (ETag) before reuse: an edit to a
    script shows up on the next load, without cache-busting names (meerpic's)."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", RevalidatingStatic(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "img" / "favicon.ico", media_type="image/x-icon")


def _app_shell() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def _landing() -> HTMLResponse:
    """The public page about meertex. Its share previews need absolute
    addresses, so {{BASE_URL}} is filled in here."""
    html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("{{BASE_URL}}", settings.base_url.rstrip("/")),
                        headers={"Cache-Control": "no-cache"})


@app.get("/")
def index(user: OptionalUser) -> Response:
    if user is None:
        return _landing()
    return _app_shell()


@app.get("/llms.txt")
def llms_txt() -> FileResponse:
    return FileResponse(STATIC_DIR / "llms.txt", media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


# Every other page of the single-page app. The client reads the path itself and
# sends a signed-out visitor to /login?next=<path>.
@app.get("/new")
@app.get("/settings")
@app.get("/timeline")
@app.get("/folders/{rest:path}")
@app.get("/p/{rest:path}")
def app_page(rest: str = "") -> FileResponse:
    return _app_shell()


# A shared timeline (DESIGN §3, Sharing the timeline). The app shows it without
# a session. Search engines stay out, and the link does not travel on in a Referer.
@app.get("/timeline/{token}")
def shared_timeline_page(token: str) -> FileResponse:
    resp = _app_shell()
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/login")
def login_page(user: OptionalUser) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(404)
async def not_found(request: Request, exc) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": getattr(exc, "detail", "Not found")}, status_code=404)
    return Response("Not found", status_code=404, media_type="text/plain")
