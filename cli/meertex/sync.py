"""The sync client, one live connection per checkout (docs/DESIGN.md §10).

Each checkout runs as one asyncio task with cooperating subtasks: the
connection (pycrdt's Provider over a WebSocket, reconnecting with backoff),
the file watcher, and the worker that runs the engine. They share one event
loop and the engine's document work is synchronous, so a remote update can
only land between two reconcile steps, never inside one. While offline the
worker keeps going: local edits go into the document and ``state.bin`` and
reach the server in the sync handshake after the next connect.

A checkout is locked (``.meertex/lock``) by the process that syncs it. Two
clients on one folder would each send the same local edit, and the text
would arrive twice. ``meertex sync --once`` on a folder that the service
already syncs asks the service for a reconcile instead and waits for its
answer in ``status.json``.

A held checkout (``.meertex/hold``) only receives. The engine checks the
marker in every step and the watcher reports it, so a running service follows
``meertex hold`` and ``meertex release`` within a second.
"""

import asyncio
import fcntl
import json
import logging
import os
import signal
import stat
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import watchfiles
from pycrdt import Provider, create_update_message
from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)

from . import __version__, config, output
from .config import META_DIR
from .engine import Engine, Report

CONNECT_TIMEOUT = 15.0
SYNC_TIMEOUT = 60.0
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
DEBOUNCE_MS = 300
# How long --once waits for the server's sync step 1 before it sends the
# whole document itself, so edits made offline are never left behind.
STEP2_GRACE = 3.0
# Blob transfers that failed are tried again this often.
RETRY_SECONDS = 15.0
# Larger than the server's MAX_SYNC_MESSAGE_BYTES, which only limits what the
# server accepts. Its first answer carries the whole document.
MAX_RECEIVE = 256 * 1024 * 1024
REQUEST_FILE = "request"
STATUS_FILE = "status.json"
LOCK_FILE = "lock"
# Close codes and HTTP statuses after which retrying cannot help.
FATAL = {4001, 4004, 401, 403, 404}

provider_log = logging.getLogger("meertex.provider")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_url(server: str, project_id: str) -> str:
    if server.startswith("https://"):
        base = "wss://" + server[len("https://"):]
    elif server.startswith("http://"):
        base = "ws://" + server[len("http://"):]
    else:
        base = server
    return f"{base.rstrip('/')}/api/projects/{quote(project_id, safe='')}/sync"


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def error_code(exc: BaseException) -> int | None:
    exc = _leaf(exc)
    if isinstance(exc, InvalidStatus):
        return exc.response.status_code
    if isinstance(exc, ConnectionClosed) and exc.rcvd is not None:
        return exc.rcvd.code
    return None


def describe(exc: BaseException, server: str = "") -> str:
    """A sentence about why the connection failed."""
    exc = _leaf(exc)
    code = error_code(exc)
    if code in (4001, 401):
        return f"The server does not accept the token ({code}). Sign in again with meertex login"
    if code in (4004, 404, 403):
        return f"No such project on the server, or you are not a member ({code})"
    if code == 4400:
        return "The server refused a message (4400)"
    if code == 1009:
        return "A message was too large for the server (1009)"
    if isinstance(exc, InvalidStatus):
        return f"The server answered HTTP {code} instead of opening the sync connection"
    if isinstance(exc, ConnectionClosed):
        return f"The connection closed ({code})" if code else "The connection closed"
    if isinstance(exc, TimeoutError):
        return "The server did not answer in time"
    if isinstance(exc, (InvalidURI, InvalidHandshake)):
        return f"Cannot open the sync connection ({exc})"
    if isinstance(exc, OSError):
        return f"Cannot reach {server or 'the server'} ({exc.strerror or exc})"
    return f"{type(exc).__name__} ({exc})"


# --- Locks -------------------------------------------------------------------------


class CheckoutLock:
    """An flock on ``.meertex/lock``, held while a process syncs the checkout.
    The kernel drops it when the process dies, so it never goes stale."""

    def __init__(self, root: Path):
        self.path = root / META_DIR / LOCK_FILE
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def holder(root: Path) -> int | None:
        """The pid syncing ``root`` right now, or None."""
        lock = CheckoutLock(root)
        if not lock.path.exists():
            return None
        if lock.acquire():
            lock.release()
            return None
        try:
            return int(lock.path.read_text().strip() or 0) or None
        except (OSError, ValueError):
            return None


# --- Transport ---------------------------------------------------------------------


class HttpBlobStore:
    """Blob contents over HTTP (§11, Blobs)."""

    def __init__(self, server: str, token: str, project_id: str):
        self.prefix = f"/api/projects/{quote(project_id, safe='')}/blobs/"
        self.client = httpx.AsyncClient(
            base_url=server,
            headers={"Authorization": f"Bearer {token}", "User-Agent": f"meertex-cli/{__version__}"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text.strip()
            raise RuntimeError(f"{detail or resp.reason_phrase} (HTTP {resp.status_code})")

    async def upload(self, sha: str, data: bytes) -> None:
        resp = await self.client.put(self.prefix + sha, content=data,
                                     headers={"Content-Type": "application/octet-stream"})
        self._check(resp)

    async def download(self, sha: str) -> bytes:
        resp = await self.client.get(self.prefix + sha)
        self._check(resp)
        return resp.content

    async def aclose(self) -> None:
        await self.client.aclose()


class WebSocketChannel:
    """pycrdt's Channel over a websockets connection. It also tells the
    session when the server has everything we have. That is the case after
    our sync step 2, and after an update sent while nothing else was queued."""

    def __init__(self, ws, path: str, session: "Session"):
        self._ws = ws
        self._path = path
        self._session = session

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self) -> "WebSocketChannel":
        return self

    async def __anext__(self) -> bytes:
        return await self.recv()

    async def recv(self) -> bytes:
        message = await self._ws.recv()  # raises ConnectionClosed, which ends the Provider
        if isinstance(message, str):
            message = message.encode()
        self._session.touched()
        return message

    async def send(self, message: bytes) -> None:
        mark = self._session.before_send(message)
        await self._ws.send(message)
        self._session.after_send(message, mark)


# --- Log ---------------------------------------------------------------------------


def _is_file(stream) -> bool:
    try:
        return stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
    except (AttributeError, OSError, ValueError):
        return False


class Log:
    """What the client did, as lines for a person or JSON lines."""

    def __init__(self, as_json: bool = False, stream=None, quiet: bool = False):
        self.as_json = as_json
        self.stream = stream or sys.stdout
        self.quiet = quiet
        # A log file (the launchd agent's on macOS, or a redirect) needs the day
        # and the time zone as well. A terminal does not, and the journal keeps its own.
        self.dated = _is_file(self.stream)

    def _emit(self, name: str, event: str, text: str, **fields) -> None:
        if self.quiet:
            return
        if self.as_json:
            line = json.dumps({"at": now_iso(), "checkout": name, "event": event, **fields}, ensure_ascii=False)
        else:
            now = datetime.now().astimezone()
            stamp = output.paint(now.isoformat(timespec="seconds") if self.dated else now.strftime("%H:%M:%S"),
                                 output.DIM, self.stream)
            line = f"{stamp} {name}  {text}"
        print(line, file=self.stream, flush=True)

    def event(self, name: str, event: str, text: str, **fields) -> None:
        self._emit(name, event, text, **fields)

    def report(self, name: str, r: Report) -> None:
        p = lambda t, c: output.paint(t, c, self.stream)
        merged = set(r.merged)
        for path in r.sent:
            self._emit(name, "sent", f"{p('↑', output.GREEN)} {path}" + (" (merged)" if path in merged else ""),
                       path=path, merged=path in merged)
        for path in r.added:
            self._emit(name, "added", f"{p('↑', output.GREEN)} {path} (new)", path=path)
        for path in r.deleted:
            self._emit(name, "deleted", f"{p('↑', output.GREEN)} {path} (deleted)", path=path)
        for old, new in r.renamed:
            self._emit(name, "renamed", f"{p('↑', output.GREEN)} {old} → {new}", path=new, old_path=old)
        for path in r.written:
            self._emit(name, "written", f"{p('↓', output.CYAN)} {path}", path=path)
        for path in r.conflicts:
            self._emit(name, "conflict", f"{p('↓', output.AMBER)} {path} (conflict copy)", path=path)
        for path in r.kept:
            self._emit(name, "kept", f"{p('↓', output.CYAN)} {path} (around your held change)", path=path)
        for path in r.removed:
            self._emit(name, "removed", f"{p('↓', output.CYAN)} {path} (renamed by someone else)", path=path)
        for path in r.trashed:
            self._emit(name, "trashed", f"{p('↓', output.CYAN)} {path} (deleted, moved to {META_DIR}/trash)",
                       path=path)
        for message in r.warnings:
            self._emit(name, "warning", p("⚠ ", output.AMBER) + message, message=message)
        for message in r.errors:
            self._emit(name, "error", p("✗ ", output.RED) + message, message=message)


# --- One checkout ------------------------------------------------------------------


class Session:
    """The sync client of one checkout."""

    def __init__(self, root: Path, token: str, log: Log):
        self.root = root.resolve()
        meta = config.read_project(self.root)
        if not meta or not meta.get("project_id"):
            raise config.ConfigError(f"{self.root} is not a meertex checkout (no {META_DIR}/project.json).")
        self.project_id: str = meta["project_id"]
        self.name: str = meta.get("name") or self.root.name
        self.server: str = config.normalize_server(meta.get("server") or config.server())
        self.token = token
        self.log = log
        self.url = sync_url(self.server, self.project_id)
        self.blobs = HttpBlobStore(self.server, token, self.project_id)
        self.engine = Engine(self.root, main_file=meta.get("main_file") or "main.tex", blobs=self.blobs)
        self.engine.load()
        self.engine.clean_tmp()
        self.engine.observe(self._remote_changed, self._local_changed)

        self.mode = "once"  # or "service", in status.json for ``sync --once`` to read
        self._logged_held = False
        self.connected = False
        self.last_sync_at: str | None = None
        self.last_error: str | None = None
        self.fatal = False
        self.provider: Provider | None = None
        self.report = Report()
        # Unsent tracking. Each local edit bumps local_seq. A send that carries
        # everything up to some local_seq moves sent_seq there.
        self.local_seq = 0
        self.sent_seq = 0
        self._step2 = asyncio.Event()
        self._wake = asyncio.Event()
        self._local_paths: set[str] = set()
        self._full = True
        self._remote = False
        self._request: str | None = None
        self.answered: str | None = None
        self._status_dirty = True

    # Hooks from the engine and the channel. They run synchronously.

    def _remote_changed(self) -> None:
        self._remote = True
        self._wake.set()

    def _local_changed(self) -> None:
        self.local_seq += 1
        self._status_dirty = True

    @property
    def unsent(self) -> bool:
        return self.local_seq > self.sent_seq

    def touched(self) -> None:
        self.last_sync_at = now_iso()
        self._status_dirty = True

    def _queue_empty(self) -> bool:
        events = getattr(self.provider, "_events", None) if self.provider else None
        try:
            return events is not None and events.statistics().current_buffer_used == 0
        except Exception:  # noqa: BLE001 - private API of pycrdt, be careful
            return False

    def before_send(self, message: bytes) -> int | None:
        if len(message) >= 2 and message[0] == 0:
            if message[1] == 1:  # sync step 2 carries everything the server lacks
                return self.local_seq
            if message[1] == 2 and self._queue_empty():
                return self.local_seq
        return None

    def after_send(self, message: bytes, mark: int | None) -> None:
        self.touched()
        if mark is not None:
            self.sent_seq = max(self.sent_seq, mark)
        if len(message) >= 2 and message[:2] == b"\x00\x01":
            self._step2.set()

    # Status.

    def status(self) -> dict:
        return {
            "pid": os.getpid(),
            "mode": self.mode,
            "project_id": self.project_id,
            "name": self.name,
            "dir": str(self.root),
            "server": self.server,
            "connected": self.connected,
            "last_sync_at": self.last_sync_at,
            "unsent": self.unsent,
            "pending": self.engine.pending,
            "last_error": self.last_error,
            "request": self.answered,
            "files": len(self.engine.shadows),
            **self.held_status(),
            "updated_at": now_iso(),
        }

    def held_status(self) -> dict:
        since = config.held_since(self.root)
        return {"held": since is not None, "held_since": since or None,
                "held_files": len(self.engine.held_paths) if since is not None else 0}

    def write_status(self) -> None:
        self._status_dirty = False
        try:
            config.write_json(self.root / META_DIR / STATUS_FILE, self.status())
        except OSError:
            pass

    async def close(self) -> None:
        await self.blobs.aclose()

    # Reconcile.

    async def _reconcile(self, paths: set[str] | None) -> Report:
        try:
            report = await self.engine.reconcile(paths)
        except Exception as exc:  # noqa: BLE001 - a bug must not stop the client
            self.last_error = f"Reconcile failed ({type(exc).__name__}, {exc})"
            self.log.event(self.name, "error", output.paint("✗ ", output.RED) + self.last_error,
                           message=self.last_error)
            return Report(errors=[self.last_error])
        self.log.report(self.name, report)
        self.report.extend(report)
        self._status_dirty = True
        if self.engine.held != self._logged_held:
            self._logged_held = self.engine.held
            if self.engine.held:
                self.log.event(self.name, "held", output.paint("held", output.AMBER)
                               + ", local changes stay here until meertex release")
            else:
                self.log.event(self.name, "released", output.paint("released", output.GREEN)
                               + ", the held changes go out")
        return report

    # The connection.

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _connect(self):
        return connect(self.url, additional_headers=self._headers(), max_size=MAX_RECEIVE,
                       open_timeout=CONNECT_TIMEOUT, user_agent_header=f"meertex-cli/{__version__}")

    async def _handshake(self, provider: Provider, channel: WebSocketChannel) -> None:
        """Wait for the initial sync. Then make sure the server got what we
        have. Our answer to its step 1 carries it. Without such an answer the
        whole document goes out as one update."""
        await asyncio.wait_for(provider.synced.wait(), SYNC_TIMEOUT)
        if self.unsent:
            try:
                await asyncio.wait_for(self._step2.wait(), STEP2_GRACE)
            except TimeoutError:
                mark = self.local_seq
                await channel.send(create_update_message(self.engine.doc.get_update()))
                self.sent_seq = max(self.sent_seq, mark)

    async def run_once(self) -> bool:
        """Reconcile, connect, sync, reconcile again, flush, save. Returns
        whether the checkout and the server are in step afterwards."""
        self.report = Report()
        ok = False
        await self._reconcile(None)  # what changed while nothing ran goes into the document first
        try:
            async with self._connect() as ws:
                channel = WebSocketChannel(ws, self.url, self)
                async with Provider(self.engine.doc, channel, log=provider_log) as provider:
                    self.provider = provider
                    self._step2.clear()
                    await self._handshake(provider, channel)
                    self.connected = True
                    for _ in range(5):
                        self._remote = False
                        await self._reconcile(None)
                        if not self._remote:
                            break
                        await asyncio.sleep(0.1)
                # Leaving the Provider's context sends what is still queued.
                self.provider = None
            ok = not self.engine.pending and not self.unsent
            if self.engine.pending:
                self.last_error = "Some files wait for a blob transfer. Run meertex sync again."
            else:
                self.last_error = None
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            self.last_error = describe(exc, self.server)
            self.fatal = error_code(exc) in FATAL
        finally:
            self.provider = None
            self.connected = False
            self.engine.save()
            self.write_status()
        return ok

    async def _connection(self, closing: asyncio.Event) -> None:
        delay = BACKOFF_START
        while not closing.is_set():
            synced = False
            try:
                async with self._connect() as ws:
                    channel = WebSocketChannel(ws, self.url, self)
                    async with Provider(self.engine.doc, channel, log=provider_log) as provider:
                        self.provider = provider
                        self._step2.clear()
                        await self._handshake(provider, channel)
                        synced = True
                        self.connected = True
                        self.last_error = None
                        delay = BACKOFF_START
                        self.log.event(self.name, "connected", output.paint("connected", output.GREEN))
                        self._full = True  # catch up on anything that changed while offline
                        self._wake.set()
                        self.write_status()
                        await closing.wait()
            except Exception as exc:  # noqa: BLE001 - every failure means trying again later
                self.last_error = describe(exc, self.server)
                if error_code(exc) in FATAL:
                    delay = BACKOFF_MAX
            finally:
                self.provider = None
                was_connected, self.connected = self.connected, False
                self.write_status()
            if closing.is_set():
                break
            if was_connected or not synced:
                self.log.event(self.name, "offline",
                               output.paint("offline", output.AMBER)
                               + f" ({self.last_error}), retrying in {int(delay)} s",
                               message=self.last_error, retry_in=delay)
            try:
                await asyncio.wait_for(closing.wait(), delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, BACKOFF_MAX)

    # The watcher and the worker.

    def _seen(self, paths: set[str]) -> None:
        meta_prefix = META_DIR + "/"
        added = False
        for rel in paths:
            if rel == f"{META_DIR}/{REQUEST_FILE}":
                self._request_seen()
                continue
            if rel == f"{META_DIR}/{config.HOLD_FILE}":
                self._full = True  # held or released: the next step follows the marker
                added = True
                continue
            if rel == META_DIR or rel.startswith((meta_prefix, "../")):
                continue
            if rel not in self.engine.shadows and self.engine.ignored_path(rel):
                continue
            self._local_paths.add(rel)
            added = True
        if added:
            self._wake.set()

    def _request_seen(self) -> None:
        try:
            nonce = (self.root / META_DIR / REQUEST_FILE).read_text().strip()
        except OSError:
            return
        if nonce and nonce != self.answered:
            self._request = nonce
            self._full = True
            self._wake.set()

    async def _watch(self, stop: asyncio.Event) -> None:
        root = str(self.root)
        meta = os.path.join(root, META_DIR)
        signals = {os.path.join(meta, REQUEST_FILE), os.path.join(meta, config.HOLD_FILE)}

        def wanted(_change, path: str) -> bool:
            # The client's own files change all the time. Only a request or the hold matters.
            return path in signals or not (path == meta or path.startswith(meta + os.sep))

        while not stop.is_set():
            try:
                async for changes in watchfiles.awatch(
                    root, debounce=DEBOUNCE_MS, step=50, stop_event=stop, watch_filter=wanted,
                    recursive=True, ignore_permission_denied=True,
                ):
                    self._seen({os.path.relpath(path, root).replace(os.sep, "/") for _c, path in changes})
            except Exception as exc:  # noqa: BLE001 - fall back to scanning, then watch again
                self.log.event(self.name, "warning",
                               output.paint("⚠ ", output.AMBER) + f"Watching files failed ({exc})",
                               message=str(exc))
                self._full = True
                self._wake.set()
                try:
                    await asyncio.wait_for(stop.wait(), 5)
                except TimeoutError:
                    pass

    async def _worker(self, stop: asyncio.Event) -> None:
        while True:
            if not (self._full or self._local_paths or self._remote or stop.is_set()):
                timeout = RETRY_SECONDS if self.engine.pending else None
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                except TimeoutError:
                    self._full = True
            self._wake.clear()
            if self._remote and not self._local_paths and not self._full and not stop.is_set():
                await asyncio.sleep(0.05)  # let a burst of remote updates land first
            full, paths = self._full, self._local_paths
            self._full, self._local_paths, self._remote = False, set(), False
            request, self._request = self._request, None
            if full or paths or not stop.is_set():
                await self._reconcile(None if full else paths)
            if request:
                self.answered = request
            if self._status_dirty or request:
                self.write_status()
            if stop.is_set():
                return

    async def _status_loop(self, closing: asyncio.Event) -> None:
        while not closing.is_set():
            if self._status_dirty:
                self.write_status()
            try:
                await asyncio.wait_for(closing.wait(), 1.0)
            except TimeoutError:
                pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sync until ``stop`` is set. The last reconcile runs before the
        connection closes, so its edits still go out."""
        self.mode = "service"
        closing = asyncio.Event()

        async def stopper() -> None:
            await stop.wait()
            self._wake.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._connection(closing))
            tg.create_task(self._watch(stop))
            tg.create_task(self._status_loop(closing))
            waker = tg.create_task(stopper())
            try:
                await self._worker(stop)
            finally:
                closing.set()
                waker.cancel()
        self.engine.save()
        self.write_status()


# --- Running checkouts -------------------------------------------------------------


async def ask_service(root: Path, timeout: float = 60.0) -> dict:
    """Another process syncs ``root``. Ask it for a reconcile through
    ``.meertex/request`` and wait until its status answers."""
    nonce = uuid.uuid4().hex
    config.write_atomic(root / META_DIR / REQUEST_FILE, (nonce + "\n").encode())
    status_path = root / META_DIR / STATUS_FILE
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        try:
            status = json.loads(status_path.read_text())
        except (OSError, ValueError):
            status = {}
        if status.get("request") == nonce:
            if status.get("connected") and not status.get("unsent") and not status.get("pending"):
                return {"ok": True, "via_service": True, "status": status}
            if not status.get("connected"):
                break
        await asyncio.sleep(0.2)
    error = status.get("last_error") or "The sync service did not answer in time."
    return {"ok": False, "via_service": True, "status": status, "error": error}


def _service_holds(root: Path) -> bool:
    """Whether the process holding the lock is a sync service that answers requests."""
    holder = CheckoutLock.holder(root)
    try:
        status = json.loads((root / META_DIR / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return False
    return holder is not None and status.get("pid") == holder and status.get("mode") == "service"


async def sync_once(root: Path, token: str, log: Log, wait: float = 120.0) -> dict:
    """``meertex sync --once`` for one checkout. When a service syncs it, the
    service is asked. When another ``--once`` does, this one waits its turn."""
    root = root.resolve()
    lock = CheckoutLock(root)
    deadline = time.monotonic() + wait
    while not lock.acquire():
        if _service_holds(root) or time.monotonic() > deadline:
            result = await ask_service(root)
            result |= {k: result["status"].get(k) for k in ("held", "held_since", "held_files")}
            result["dir"] = str(root)
            meta = config.read_project(root) or {}
            result["name"] = meta.get("name") or root.name
            return result
        await asyncio.sleep(0.2)
    session = None
    try:
        session = Session(root, token, log)
        ok = await session.run_once()
        return {"dir": str(root), "name": session.name, "project_id": session.project_id, "ok": ok,
                "error": session.last_error, "fatal": session.fatal, "report": session.report.as_dict(),
                **session.held_status()}
    finally:
        if session is not None:
            await session.close()
        lock.release()


async def sync_many_once(roots: list[Path], token: str, log: Log) -> list[dict]:
    return list(await asyncio.gather(*(sync_once(r, token, log) for r in roots)))


async def run_daemon(dirs: list[Path] | None, log: Log, stop: asyncio.Event | None = None,
                     poll: float = 5.0) -> int:
    """``meertex sync`` runs every registered checkout (or ``dirs``) until a
    signal. New registrations are picked up while it runs."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    running: dict[Path, tuple[asyncio.Task, asyncio.Event, CheckoutLock, Session]] = {}
    said: set[str] = set()

    def once(key: str, name: str, event: str, text: str) -> None:
        if key not in said:
            said.add(key)
            log.event(name, event, text)

    try:
        while not stop.is_set():
            token = config.token(required=False)
            if dirs is not None:
                wanted = [d.resolve() for d in dirs]
            else:
                try:
                    wanted = [Path(c["dir"]) for c in config.load_checkouts()]
                except config.ConfigError as exc:
                    once(str(exc), "meertex", "error", str(exc))
                    wanted = list(running)
            if not token:
                once("token", "meertex", "waiting", "Not signed in. Waiting for meertex login.")
            elif not wanted:
                once("none", "meertex", "waiting", "No checkouts yet. Waiting for meertex checkout or new --checkout.")
            for root in wanted:
                if root in running or not token:
                    continue
                if not (root / META_DIR / config.PROJECT_FILE).is_file():
                    once(f"missing:{root}", root.name, "warning", f"{root} is not a checkout (any more). Skipped.")
                    continue
                lock = CheckoutLock(root)
                if not lock.acquire():
                    once(f"locked:{root}", root.name, "warning",
                         f"Another process (pid {CheckoutLock.holder(root)}) syncs {root}. Waiting for it.")
                    continue
                try:
                    session = Session(root, token, log)
                except (config.ConfigError, OSError, ValueError) as exc:
                    lock.release()
                    once(f"bad:{root}", root.name, "error", str(exc))
                    continue
                said.discard(f"locked:{root}")
                session_stop = asyncio.Event()
                task = asyncio.create_task(session.run_forever(session_stop))
                running[root] = (task, session_stop, lock, session)
                log.event(session.name, "started", f"syncing {root}")
            for root in list(running):
                task, session_stop, lock, session = running[root]
                if root not in wanted or not (root / META_DIR / config.PROJECT_FILE).is_file():
                    session_stop.set()
                elif token and token != session.token:
                    session_stop.set()  # signed in again: restart with the new token
                if task.done():
                    if not task.cancelled() and task.exception() is not None:
                        log.event(session.name, "error", f"stopped ({task.exception()})")
                    await session.close()
                    lock.release()
                    del running[root]
            if dirs is not None and not running and all(f"missing:{d.resolve()}" in said for d in dirs):
                return 1
            try:
                await asyncio.wait_for(stop.wait(), poll)
            except TimeoutError:
                pass
    finally:
        for _task, session_stop, _lock, _session in running.values():
            session_stop.set()
        if running:
            await asyncio.wait([t for t, *_ in running.values()], timeout=20)
        for task, _stop, lock, session in running.values():
            if not task.done():
                task.cancel()
            await session.close()
            lock.release()
    return 0
