"""The sync endpoint: y-websocket on GET /api/projects/{ref}/sync (docs/DESIGN.md §10).

The protocol is plain y-websocket, so the browser's provider and pycrdt's
Provider work unchanged. A message is ``[varuint type, ...]``:

* ``[0, 0, state vector]`` is sync step 1. The server sends its own on
  connect and answers the client's with step 2.
* ``[0, 1, update]`` (step 2) and ``[0, 2, update]`` (update) are applied to
  the room, stored, and sent to every other connection of the project.
  Reviewers and viewers are read-only, their step 2 and updates are dropped.
* ``[1, awareness update]`` carries cursors and names. The room keeps the
  states, so a new connection sees who is there, and relays the message.
* ``[3]`` asks for every awareness state (y-protocols' query message).

Each connection is two tasks on the event loop. The receiver handles one
message after the other and hands every document access to a worker thread
(rooms.py explains why). The sender drains the connection's outbox, which the
room fills from any thread. Every send goes through the outbox, so one task
writes the socket and a close code goes out after everything queued before it.
"""

import contextlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pycrdt import (
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    write_message,
    write_var_uint,
)

from ..access import can, resolve_project
from ..config import get_settings
from ..database import SessionLocal
from ..live import rooms
from ..security import SESSION_COOKIE, get_optional_user_or_token

router = APIRouter(prefix="/api/projects", tags=["sync"])

QUERY_AWARENESS = 3

CLOSE_NOT_SIGNED_IN = 4001
CLOSE_NO_PROJECT = rooms.CLOSE_NO_PROJECT
CLOSE_INVALID = 4400

# Awareness client ids one connection may announce. A browser tab has one.
MAX_CLIENT_IDS = 16


class ProtocolError(ValueError):
    pass


# --- Decoding (lib0's varuint and length-prefixed bytes) --------------------------


def _var_uint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ProtocolError("A number is cut off")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def _var_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    length, pos = _var_uint(data, pos)
    end = pos + length
    if end > len(data):
        raise ProtocolError("A payload is cut off")
    return data[pos:end], end


def _last_bytes(data: bytes, pos: int) -> bytes:
    """The length-prefixed payload that ends the message."""
    payload, end = _var_bytes(data, pos)
    if end != len(data):
        raise ProtocolError("Bytes after the payload")
    return payload


def _sync_message(step: int, payload: bytes) -> bytes:
    return bytes([YMessageType.SYNC, step]) + write_message(payload)


# --- Awareness ---------------------------------------------------------------------


def _signed(state: str, conn: rooms.Connection) -> str:
    """The state with the signed-in person's name, email and picture in its
    ``user`` field, whatever the client wrote there. The editor shows these
    next to cursors, so nobody can appear as someone else."""
    value = json.loads(state)
    if isinstance(value, dict) and isinstance(value.get("user"), dict):
        value["user"] = {**value["user"], "name": conn.name, "email": conn.editor, "avatar_url": conn.avatar_url}
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return state


def _apply_awareness(room: rooms.Room, conn: rooms.Connection, update: bytes) -> bytes:
    """Keep the room's awareness states up to date, by y-protocols' rules: a
    state wins with a higher clock, and a removal (state null) also with the
    same clock. Returns the message to pass on, with the user fields signed.
    Raises ValueError for a malformed update."""
    count, pos = _var_uint(update, 0)
    entries = []
    for _ in range(count):
        client_id, pos = _var_uint(update, pos)
        clock, pos = _var_uint(update, pos)
        raw, pos = _var_bytes(update, pos)
        state = _signed(raw.decode(), conn)
        entries.append((client_id, clock, state, json.loads(state) is None))
    if pos != len(update):
        raise ProtocolError("Bytes after the awareness update")
    for client_id, clock, state, removed in entries:
        known = room.awareness.get(client_id)
        known_clock = known[0] if known else 0
        if not (known_clock < clock or (removed and known is not None and known_clock == clock)):
            continue
        if removed:
            room.awareness.pop(client_id, None)
            conn.client_ids.discard(client_id)
        else:
            room.awareness[client_id] = (clock, state)
            conn.client_ids.add(client_id)
    if len(conn.client_ids) > MAX_CLIENT_IDS:
        raise ProtocolError("Too many awareness clients on one connection")
    return _awareness_message([(client_id, clock, state) for client_id, clock, state, _removed in entries])


def _awareness_message(entries: list[tuple[int, int, str]]) -> bytes:
    update = bytearray(write_var_uint(len(entries)))
    for client_id, clock, state in entries:
        raw = state.encode()
        update += write_var_uint(client_id) + write_var_uint(clock) + write_var_uint(len(raw)) + raw
    return create_awareness_message(bytes(update))


def _send_awareness(room: rooms.Room, conn: rooms.Connection) -> None:
    """Every awareness state the room knows, to one connection."""
    if room.awareness:
        conn.send(_awareness_message([(cid, clock, state) for cid, (clock, state) in room.awareness.items()]))


def _forget_awareness(room: rooms.Room, conn: rooms.Connection) -> None:
    """Remove the states of a connection that left, for everybody."""
    gone = []
    for client_id in conn.client_ids:
        known = room.awareness.pop(client_id, None)
        if known is not None:
            gone.append((client_id, known[0], "null"))
    if gone:
        message = _awareness_message(gone)
        for other in room.connections:
            if other is not conn:
                other.send_threadsafe(message)


# --- Who is connecting -------------------------------------------------------------


@dataclass
class _Access:
    project_id: str
    editor: str
    can_write: bool
    name: str
    avatar_url: str | None = None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _session_cookie(websocket: WebSocket) -> str | None:
    """The session cookie, but only when the page that opened the socket is
    ours. CORS does not cover WebSockets, so without this check any site
    could open one with a signed-in visitor's cookie."""
    cookie = websocket.cookies.get(SESSION_COOKIE)
    origin = websocket.headers.get("origin")
    if cookie is None or origin is None:  # not a browser
        return cookie
    allowed = {_origin(get_settings().base_url)}
    host = websocket.headers.get("host")
    if host:
        allowed |= {f"http://{host}", f"https://{host}"}
    return cookie if origin.rstrip("/") in allowed else None


def _authorize(project: str, cookie: str | None, authorization: str | None, token: str | None) -> _Access | int:
    """The project and the role, or the close code."""
    with SessionLocal() as db:
        user = get_optional_user_or_token(db, cookie, token, authorization)
        if user is None:
            return CLOSE_NOT_SIGNED_IN
        try:
            p, membership = resolve_project(db, user, project)
        except HTTPException:
            return CLOSE_NO_PROJECT
        return _Access(p.id, user.email, can(membership, "editor"), user.name or user.email, user.avatar_url)


# --- The connection ----------------------------------------------------------------


async def _handle(data: bytes, room: rooms.Room, conn: rooms.Connection) -> None:
    """One message. Raises ValueError for one that is not valid."""
    kind, pos = _var_uint(data, 0)
    if kind == YMessageType.SYNC:
        step, pos = _var_uint(data, pos)
        payload = _last_bytes(data, pos)
        if step == YSyncMessageType.SYNC_STEP1:
            update = await anyio.to_thread.run_sync(room.update_since, payload)
            conn.send(_sync_message(YSyncMessageType.SYNC_STEP2, update))
        elif step in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE):
            if conn.can_write:
                await anyio.to_thread.run_sync(room.apply, payload, conn)
        else:
            raise ProtocolError(f"Unknown sync message {step}")
    elif kind == YMessageType.AWARENESS:
        message = _apply_awareness(room, conn, _last_bytes(data, pos))
        for other in room.connections:
            if other is not conn:
                other.send_threadsafe(message)
    elif kind == QUERY_AWARENESS:
        if pos != len(data):
            raise ProtocolError("Bytes after the query")
        _send_awareness(room, conn)
    else:
        raise ProtocolError(f"Unknown message type {kind}")


async def _receive(websocket: WebSocket, room: rooms.Room, conn: rooms.Connection) -> bool:
    """Handle messages until the client leaves (True) or a close is queued (False)."""
    while not conn.closing:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return True
        data = message.get("bytes")
        if data is None:
            conn.close(CLOSE_INVALID, "Messages are binary")
        elif len(data) > get_settings().max_sync_message_bytes:
            conn.close(CLOSE_INVALID, "The message is too large")
        else:
            try:
                await _handle(data, room, conn)
            except ValueError:
                conn.close(CLOSE_INVALID, "Not a valid y-websocket message")
    return False


async def _send(websocket: WebSocket, conn: rooms.Connection, scope: anyio.CancelScope) -> None:
    """Write the outbox to the socket until a close comes out of it, then end
    the connection's other task too."""
    try:
        # Either error means the client is gone.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                item = await conn.outbox.get()
                if isinstance(item, tuple):
                    await websocket.close(*item)
                    return
                await websocket.send_bytes(item)
                conn.sent(item)
    finally:
        scope.cancel()


@router.websocket("/{project}/sync")
async def sync(websocket: WebSocket, project: str) -> None:
    # Accept first: a close code only reaches the client after the handshake.
    await websocket.accept()
    access = await anyio.to_thread.run_sync(
        _authorize,
        project,
        _session_cookie(websocket),
        websocket.headers.get("authorization"),
        websocket.query_params.get("token"),
    )
    if isinstance(access, int):
        reason = "Not signed in" if access == CLOSE_NOT_SIGNED_IN else "No such project"
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.close(access, reason)
        return

    conn = rooms.Connection(access.editor, access.can_write, access.name, access.avatar_url)
    room = await anyio.to_thread.run_sync(rooms.join, access.project_id, conn)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_send, websocket, conn, tg.cancel_scope)
            state = await anyio.to_thread.run_sync(room.state)
            conn.send(_sync_message(YSyncMessageType.SYNC_STEP1, state))
            _send_awareness(room, conn)
            if await _receive(websocket, room, conn):
                tg.cancel_scope.cancel()
    finally:
        _forget_awareness(room, conn)
        # Store what it sent right away, even when the server is shutting down.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(room.leave, conn)
