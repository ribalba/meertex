"""Helpers for the live slice's tests: users, projects and a sync client.

Projects are made as rows here, so these tests do not depend on the projects
API. The sync client is a pycrdt Doc on a TestClient WebSocket that speaks the
same y-websocket messages as the CLI and the browser.
"""

import time
import uuid

import anyio
from pycrdt import (
    Doc,
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)


def make_user(email: str | None = None) -> tuple[str, str, str]:
    """A user with an API token: (id, email, token)."""
    from app.database import SessionLocal
    from app.models import User
    from app.security import new_api_token

    with SessionLocal() as db:
        user = User(id=str(uuid.uuid4()), email=email or f"{uuid.uuid4().hex[:8]}@example.com", api_token=new_api_token())
        db.add(user)
        db.commit()
        return user.id, user.email, user.api_token or ""


def make_project(owner_id: str, members: dict[str, str] | None = None, template: str = "empty") -> tuple[str, str]:
    """A project owned by ``owner_id`` with ``members`` ({user id: role}), its
    repository and its live document. Returns (id, name)."""
    from app import live
    from app.database import SessionLocal
    from app.models import Membership, Project

    pid, name = str(uuid.uuid4()), f"p-{uuid.uuid4().hex[:6]}"
    with SessionLocal() as db:
        db.add(Project(id=pid, owner_id=owner_id, name=name))
        db.flush()
        db.add(Membership(project_id=pid, user_id=owner_id, role="owner"))
        for user_id, role in (members or {}).items():
            db.add(Membership(project_id=pid, user_id=user_id, role=role))
        db.commit()
    live.init_project(pid, template)
    return pid, name


def bare_client():
    """A client without a session cookie, for users who sign in with a token.
    Its WebSockets run on event loops of their own, which also shows that a
    room reaches connections on any loop."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class Closed(Exception):
    """The server closed the connection with ``code``."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class Peer:
    """A sync client: a pycrdt Doc on a TestClient WebSocket."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.doc = Doc()
        self.received: list[bytes] = []

    def recv(self, timeout: float = 5.0) -> bytes:
        """The next message. Raises Closed when the server closed, TimeoutError
        when nothing came."""

        async def receive():
            with anyio.fail_after(timeout):
                return await self.ws._send_rx.receive()

        message = self.ws.portal.call(receive)
        if message["type"] == "websocket.close":
            raise Closed(message.get("code", 1000))
        self.received.append(message["bytes"])
        return message["bytes"]

    def handle(self, message: bytes) -> None:
        if message[0] == YMessageType.SYNC:
            reply = handle_sync_message(message[1:], self.doc)
            if reply is not None:
                self.ws.send_bytes(reply)

    def sync(self) -> None:
        """Send sync step 1 and handle messages until the server's answer came.
        The server handles messages in order, so everything sent before is
        applied by then."""
        self.ws.send_bytes(create_sync_message(self.doc))
        while True:
            message = self.recv()
            self.handle(message)
            if message[:2] == bytes([YMessageType.SYNC, YSyncMessageType.SYNC_STEP2]):
                return

    def drain(self, quiet: float = 0.3) -> None:
        """Handle messages until none came for ``quiet`` seconds."""
        while True:
            try:
                self.handle(self.recv(timeout=quiet))
            except TimeoutError:
                return

    def update_since(self, state: bytes) -> bytes:
        return create_update_message(self.doc.get_update(state))

    def edit(self, change) -> bytes:
        """Change the local document and return the update message, unsent."""
        before = self.doc.get_state()
        change(self.doc)
        return self.update_since(before)

    def push(self, change) -> None:
        self.ws.send_bytes(self.edit(change))

    def close_code(self, timeout: float = 5.0) -> int:
        """Read until the server closes, and return the close code."""
        while True:
            try:
                self.recv(timeout)
            except Closed as closed:
                return closed.code
