"""Rooms: one in-memory live document per open project (docs/DESIGN.md §8).

Every change to a project's document goes through its room: sync connections,
API writes and snapshots alike. That is what keeps one copy of the document per
project in this process. A second copy loaded on the side would miss the
other's edits until it was reloaded.

A room is used from worker threads and from the event loop. The API's plain
``def`` routes, snapshots and database writes run in threads. The sync
WebSockets (routers/sync.py) run on the event loop, and they hand every
document access to a thread as well, so the loop never waits for a room while
an API write diffs a long file. ``Room.lock`` (re-entrant) is held for every
read or write of ``Room.doc`` and only for that. Git and database work happen
outside it.

Every change, wherever it was made, ends in ``Room._on_transaction``. That runs
inside the transaction's commit, so under the lock and in commit order. It
queues the update for storage and for every sync connection except the one it
came from. A connection's outbox belongs to its event loop, so the update gets
there through ``loop.call_soon_threadsafe``, which keeps the order.

Storage is batched. Updates from sync connections are written every
FLUSH_SECONDS as one row, and right away when a connection closes. API writes
are written before the request returns (``Room.edit``).

Deadlines get a snapshot of their own. Snapshots otherwise wait for a quiet
minute, and a version frozen "at the deadline" takes the last commit before
it, so the edits of the last minute would be missing without one.
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from functools import partial

import anyio
from anyio.abc import TaskGroup
from pycrdt import Doc, TransactionEvent, create_update_message, merge_updates
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import SessionLocal
from ..models import ProjectDate, utcnow
from . import store, ydoc

logger = logging.getLogger("uvicorn.error")

# How often updates from sync connections are stored.
FLUSH_SECONDS = 0.5
# How often the snapshot and unload timers are looked at.
TICK_SECONDS = 0.5
# How often the dates are searched for deadlines coming up, and how far ahead.
DEADLINE_SCAN_SECONDS = 60.0
DEADLINE_AHEAD = timedelta(seconds=120)

# WebSocket close codes the rooms use (docs/DESIGN.md §10).
CLOSE_NO_PROJECT = 4004
# A member's role changed. Clients connect again and get the new rights.
CLOSE_ROLE_CHANGED = 4009
CLOSE_RESTART = 1012
# A client that stops reading. It connects again and gets what it missed through
# the sync handshake, so nothing is lost by dropping what was queued for it.
CLOSE_TRY_AGAIN = 1013

# Bytes queued for one connection before it counts as not reading. The first
# sync of a big project is one message of a few MB, so this sits well above.
MAX_OUTBOX_BYTES = 128 * 1024 * 1024


class Connection:
    """A sync WebSocket as its room sees it: who is on it, whether they may
    write, and the outbox that routers/sync.py drains onto the socket.

    Only the connection's event loop touches the outbox. Other threads use
    ``send_threadsafe`` and ``close_threadsafe``."""

    def __init__(self, editor: str, can_write: bool, name: str | None = None, avatar_url: str | None = None) -> None:
        self.editor = editor
        self.can_write = can_write
        # The signed-in person's name and picture, which replace whatever the
        # client puts into its awareness state (routers/sync.py).
        self.name = name or editor
        self.avatar_url = avatar_url
        self.loop = asyncio.get_running_loop()
        # Messages to send, ended by at most one (code, reason) that closes the socket.
        self.outbox: asyncio.Queue[bytes | tuple[int, str]] = asyncio.Queue()
        self.queued_bytes = 0
        self.closing = False
        # Awareness client ids this connection announced. They are removed for
        # everybody when it leaves.
        self.client_ids: set[int] = set()

    def send(self, message: bytes) -> None:
        if self.closing:
            return
        self.queued_bytes += len(message)
        if self.queued_bytes > MAX_OUTBOX_BYTES:
            # It does not read. Drop the backlog and close, it resyncs later.
            while not self.outbox.empty():
                self.outbox.get_nowait()
            self.queued_bytes = 0
            self.close(CLOSE_TRY_AGAIN, "Too far behind, connect again")
            return
        self.outbox.put_nowait(message)

    def sent(self, message: bytes) -> None:
        """The sender wrote ``message`` to the socket."""
        self.queued_bytes = max(0, self.queued_bytes - len(message))

    def close(self, code: int, reason: str = "") -> None:
        """Close the socket after the messages queued so far."""
        if not self.closing:
            self.closing = True
            self.outbox.put_nowait((code, reason))

    def send_threadsafe(self, message: bytes) -> None:
        self._call_soon(self.send, message)

    def close_threadsafe(self, code: int, reason: str = "") -> None:
        self._call_soon(self.close, code, reason)

    def _call_soon(self, fn: Callable[..., None], *args: object) -> None:
        # A closed loop took the connection with it, so there is nobody to tell.
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(fn, *args)


class Room:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.lock = threading.RLock()
        self.doc, self._stored_rows = store.load(project_id)
        # Updates made since the last flush, in order.
        self._pending: list[bytes] = []
        # One flush at a time, so a compaction never deletes a row that was
        # written after it took the document's state.
        self._flush_lock = threading.Lock()
        # Who edited since the last snapshot, by email.
        self.editors: set[str] = set()
        # time.monotonic() of the first and the last edit not yet in a commit.
        self.dirty_since: float | None = None
        self.last_edit: float | None = None
        # time.monotonic() of the last read, edit or connection, for unloading.
        self.last_used = time.monotonic()
        # The open sync connections. Replaced, never changed in place, so the
        # event loop can walk it without the lock.
        self.connections: frozenset[Connection] = frozenset()
        # Awareness (cursors, names) by client id: (clock, state as JSON).
        # Only the event loop uses it (routers/sync.py).
        self.awareness: dict[int, tuple[int, str]] = {}
        # An unloaded or deleted room. Whoever still holds it is sent on to the
        # room that replaced it (see _enter).
        self.closed = False
        self.deleted = False
        # Where the change being committed comes from, for _on_transaction.
        self._origin: Connection | None = None
        self._editor: str | None = None
        self._subscription = self.doc.observe(self._on_transaction)

    def _on_transaction(self, event: TransactionEvent) -> None:
        # Called inside the transaction's commit, so under self.lock whenever
        # the writer followed the rule above. It must not raise: pycrdt would
        # fail the commit of a change that is already made.
        update = event.update
        if not update or update == b"\x00\x00":
            return
        self._pending.append(update)
        now = time.monotonic()
        if self.dirty_since is None:
            self.dirty_since = now
        self.last_edit = now
        if self._editor:
            self.editors.add(self._editor)
        if self.connections:
            message = create_update_message(update)
            for conn in self.connections:
                if conn is not self._origin:
                    conn.send_threadsafe(message)

    def _enter(self) -> "Room":
        """Take the lock of this room, or of the room that replaced it after an
        unload, and return that room. Callers may hold a Room for a while."""
        room = self
        while True:
            room.lock.acquire()
            if not room.closed:
                room.last_used = time.monotonic()
                return room
            room.lock.release()
            if room.deleted:
                raise LookupError(f"Project {room.project_id} was deleted")
            room = get(room.project_id)

    @contextmanager
    def edit(self, editor: str | None = None) -> Iterator[Doc]:
        """Change the document from synchronous code. Every sync connection
        gets the change, and it is stored before this returns."""
        room = self._enter()
        previous, room._editor = room._editor, editor
        try:
            yield room.doc
        finally:
            room._editor = previous
            room.lock.release()
        room.flush()

    @contextmanager
    def read(self) -> Iterator[Doc]:
        room = self._enter()
        try:
            yield room.doc
        finally:
            room.lock.release()

    # --- For sync connections (routers/sync.py, always from a worker thread) ---

    def state(self) -> bytes:
        """The document's state vector, for sync step 1."""
        with self.lock:
            return self.doc.get_state()

    def update_since(self, state: bytes) -> bytes:
        """Everything the holder of ``state`` lacks, for sync step 2. Raises
        ValueError when ``state`` is not a state vector."""
        with self.lock:
            return self.doc.get_update(state)

    def apply(self, update: bytes, conn: Connection) -> None:
        """Apply an update that ``conn`` sent. Every other connection gets it.
        Raises ValueError when it is not an update."""
        with self.lock:
            self._origin, self._editor = conn, conn.editor
            try:
                self.doc.apply_update(update)
            finally:
                self._origin = self._editor = None

    def leave(self, conn: Connection) -> None:
        """Remove a sync connection and store what it sent."""
        with self.lock:
            self.connections = self.connections - {conn}
            self.last_used = time.monotonic()
        self.flush()

    # --- Storage and snapshots ---

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def flush(self) -> None:
        """Store the pending updates as one row. Must not be called with
        self.lock held, because a compaction takes it inside the flush lock."""
        with self._flush_lock:
            with self.lock:
                pending, self._pending = self._pending, []
            if not pending or self.deleted:
                return
            try:
                store.append(self.project_id, merge_updates(*pending) if len(pending) > 1 else pending[0])
                self._stored_rows += 1
                if self._stored_rows > store.COMPACT_ABOVE_ROWS:
                    with self.lock:
                        full = self.doc.get_update()
                    store.compact(self.project_id, full)
                    self._stored_rows = 1
            except IntegrityError:
                # The project row is gone: the project was deleted, and
                # live.delete_project() has not come round to this room yet.
                logger.warning("Project %s no longer exists, its room is closed", self.project_id)
                _drop_room(self)
            except BaseException:
                with self.lock:
                    self._pending[:0] = pending  # the next flush tries again
                raise

    def take_for_snapshot(self) -> tuple[dict[str, ydoc.Entry], set[str]]:
        """The files as a snapshot writes them, and who edited since the last
        snapshot. From here on the room counts as clean, so an edit made while
        the commit runs starts the next dirty period."""
        with self.lock:
            files = ydoc.materialize(self.doc)
            editors, self.editors = self.editors, set()
            self.dirty_since = None
            return files, editors

    def snapshot_failed(self, editors: set[str]) -> None:
        """Undo take_for_snapshot() after the commit failed. The next attempt
        waits for SNAPSHOT_IDLE_SECONDS, so a broken repository is not retried
        on every tick."""
        with self.lock:
            self.editors |= editors
            self.dirty_since = self.last_edit = time.monotonic()

    def snapshot_due(self, now: float, idle_seconds: float, max_seconds: float) -> bool:
        if self.dirty_since is None:
            return False
        last = self.last_edit if self.last_edit is not None else self.dirty_since
        return now - last >= idle_seconds or now - self.dirty_since >= max_seconds

    def is_idle(self, now: float, idle_seconds: float) -> bool:
        return not self.connections and now - self.last_used >= idle_seconds

    def close(self) -> None:
        """Store what is pending and stop using this room."""
        self.flush()
        with self.lock:
            self._close()

    def _close(self) -> None:
        # Under self.lock. Unobserving breaks the cycle doc -> callback -> room.
        self.closed = True
        if self._subscription is not None:
            self.doc.unobserve(self._subscription)
            self._subscription = None


_rooms: dict[str, Room] = {}
_rooms_guard = threading.Lock()
# How many running() contexts are active. Tests run nested apps.
_running = [0]


def get(project_id: str) -> Room:
    """The project's room, loaded on first use."""
    with _rooms_guard:
        room = _rooms.get(project_id)
        if room is None:
            room = _rooms[project_id] = Room(project_id)
        return room


def loaded(project_id: str) -> Room | None:
    """The project's room if it is loaded, without loading it."""
    with _rooms_guard:
        return _rooms.get(project_id)


def join(project_id: str, conn: Connection) -> Room:
    """Add a sync connection to the project's room and return the room. From
    here on the connection gets every change, and the room stays loaded."""
    room = get(project_id)._enter()
    try:
        room.connections = room.connections | {conn}
    finally:
        room.lock.release()
    return room


def drop(project_id: str) -> None:
    """Forget a room without storing anything more (the project is being
    deleted). Its sync connections are closed with 4004."""
    with _rooms_guard:
        room = _rooms.get(project_id)
    if room is not None:
        _drop_room(room)


def _drop_room(room: Room) -> None:
    with _rooms_guard:
        if _rooms.get(room.project_id) is room:
            del _rooms[room.project_id]
    with room.lock:
        room.deleted = True
        room._pending = []
        connections, room.connections = room.connections, frozenset()
        room._close()
    for conn in connections:
        conn.close_threadsafe(CLOSE_NO_PROJECT, "The project was deleted")


def kick(project_id: str, email: str, code: int, reason: str) -> None:
    """Close one person's sync connections on a project. Their role was
    checked when they connected, so a removal or a new role only counts on
    open connections once they are closed."""
    with _rooms_guard:
        room = _rooms.get(project_id)
    if room is None:
        return
    for conn in room.connections:
        if conn.editor == email:
            conn.close_threadsafe(code, reason)


def open_rooms() -> list[Room]:
    with _rooms_guard:
        return list(_rooms.values())


def _snapshot(project_id: str, at: datetime | None = None) -> None:
    # Imported here because the package imports this module.
    from . import NotFound, snapshot

    # A project deleted meanwhile was logged by Room.flush() already.
    with contextlib.suppress(NotFound):
        snapshot(project_id, at=at)


def unload(room: Room) -> bool:
    """Write an idle room out (store, snapshot) and forget it. Returns False
    when it was used again meanwhile, and the room stays."""
    room.flush()
    if room.deleted:
        return True
    if room.dirty_since is not None:
        _snapshot(room.project_id)
    idle_seconds = get_settings().room_idle_seconds
    with _rooms_guard:
        # Never wait for a room while holding the guard: every get() needs it.
        if _rooms.get(room.project_id) is not room or not room.lock.acquire(blocking=False):
            return False
        try:
            if room.has_pending or room.dirty_since is not None or not room.is_idle(time.monotonic(), idle_seconds):
                return False
            del _rooms[room.project_id]
            room._close()
        finally:
            room.lock.release()
    return True


async def _flush_loop() -> None:
    while True:
        await anyio.sleep(FLUSH_SECONDS)
        for room in open_rooms():
            if not room.has_pending:
                continue
            try:
                await anyio.to_thread.run_sync(room.flush)
            except Exception:
                logger.exception("Storing the live document of project %s failed", room.project_id)


async def _timer_loop() -> None:
    while True:
        await anyio.sleep(TICK_SECONDS)
        settings = get_settings()
        for room in open_rooms():
            now = time.monotonic()
            try:
                if room.snapshot_due(now, settings.snapshot_idle_seconds, settings.snapshot_max_seconds):
                    await anyio.to_thread.run_sync(_snapshot, room.project_id)
                elif room.is_idle(now, settings.room_idle_seconds):
                    await anyio.to_thread.run_sync(unload, room)
            except Exception:
                logger.exception("Snapshot or unload of project %s failed", room.project_id)


def _deadlines_ahead() -> list[tuple[str, datetime]]:
    """(project id, due_at) of every date due within DEADLINE_AHEAD."""
    now = utcnow()
    with SessionLocal() as db:
        rows = (
            db.query(ProjectDate.project_id, ProjectDate.due_at)
            .filter(ProjectDate.due_at >= now, ProjectDate.due_at <= now + DEADLINE_AHEAD)
            .all()
        )
    return [(project_id, due_at) for project_id, due_at in rows]


async def _snapshot_at(project_id: str, due_at: datetime, scheduled: set[tuple[str, datetime]]) -> None:
    """Snapshot a project at a deadline, dated with the deadline."""
    try:
        await anyio.sleep(max(0.0, (due_at - utcnow()).total_seconds()))
        await anyio.to_thread.run_sync(partial(_snapshot, project_id, at=due_at))
    except Exception:
        logger.exception("The deadline snapshot of project %s failed", project_id)
    finally:
        scheduled.discard((project_id, due_at))


async def _deadline_loop(tg: TaskGroup) -> None:
    scheduled: set[tuple[str, datetime]] = set()
    while True:
        try:
            for deadline in await anyio.to_thread.run_sync(_deadlines_ahead):
                if deadline not in scheduled:
                    scheduled.add(deadline)
                    tg.start_soon(_snapshot_at, *deadline, scheduled)
        except Exception:
            logger.exception("Looking for deadlines failed")
        await anyio.sleep(DEADLINE_SCAN_SECONDS)


def _write_out(last: bool) -> None:
    """Store and snapshot every room. The last app to stop also closes the
    remaining connections and forgets every room."""
    for room in open_rooms():
        if last:
            for conn in room.connections:
                conn.close_threadsafe(CLOSE_RESTART, "The server is restarting")
        try:
            room.flush()
            if room.dirty_since is not None:
                _snapshot(room.project_id)
        except Exception:
            logger.exception("Writing out project %s failed", room.project_id)
    if last:
        with _rooms_guard:
            rooms = list(_rooms.values())
            _rooms.clear()
        for room in rooms:
            with room.lock:
                room._close()


@asynccontextmanager
async def running():
    """Background work for the app's lifetime: storing what sync connections
    sent, snapshots (after edits and at deadlines), unloading idle rooms, and
    writing every room out on shutdown."""
    _running[0] += 1
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_flush_loop)
            tg.start_soon(_timer_loop)
            tg.start_soon(_deadline_loop, tg)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
    finally:
        _running[0] -= 1
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(_write_out, _running[0] == 0)
