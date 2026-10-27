"""The sync client over a real WebSocket, against a small in-process server
that speaks the y-websocket protocol the way §10 describes it: sync step 1 on
connect, step 2 in answer to ours, updates relayed to the other connections.
Blobs go through a fake store, the HTTP side is covered by the e2e test.
"""

import asyncio
import json
import socket
from pathlib import Path

import pytest
from meertex import config
from meertex import ydoc as y
from meertex.sync import CheckoutLock, Log, Session, sync_once
from pycrdt import (
    Doc,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)
from websockets.asyncio.server import serve

TOKEN = "good-token"
PROJECT = "p1"


class FakeServer:
    def __init__(self, port: int):
        self.port = port
        self.doc = Doc()
        self.conns: set = set()
        self.server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def handler(self, ws) -> None:
        if ws.request.headers.get("Authorization") != f"Bearer {TOKEN}":
            await ws.close(4001, "not signed in")
            return
        if ws.request.path != f"/api/projects/{PROJECT}/sync":
            await ws.close(4004, "no such project")
            return
        self.conns.add(ws)
        try:
            await ws.send(create_sync_message(self.doc))
            async for msg in ws:
                if msg[0] != 0:
                    continue
                if msg[1] == 0:
                    await ws.send(handle_sync_message(msg[1:], self.doc))
                else:
                    update = read_message(msg[2:])
                    if update != b"\x00\x00":
                        self.doc.apply_update(update)
                        for other in list(self.conns):
                            if other is not ws:
                                await other.send(create_update_message(update))
        finally:
            self.conns.discard(ws)

    async def start(self) -> None:
        self.server = await serve(self.handler, "127.0.0.1", self.port)

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def put_text(self, path: str, text: str) -> None:
        """An edit made on the server, sent to every connection like the API's."""
        before = self.doc.get_state()
        y.put_text(self.doc, path, text)
        update = self.doc.get_update(before)
        for ws in list(self.conns):
            await ws.send(create_update_message(update))

    def text(self, path: str) -> str | None:
        e = y.materialize(self.doc).get(path)
        return e.text if e else None


class FakeBlobs:
    def __init__(self):
        self.data = {}

    async def upload(self, sha, data):
        self.data[sha] = data

    async def download(self, sha):
        return self.data[sha]

    async def aclose(self):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_checkout(root: Path, server: FakeServer) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config.write_project(root, {"project_id": PROJECT, "name": "paper", "server": server.url, "main_file": "main.tex"})
    return root


def session(root: Path, blobs: FakeBlobs, token: str = TOKEN) -> Session:
    s = Session(root, token, Log(quiet=True))
    s.blobs = s.engine.blobs = blobs
    return s


async def until(condition, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.fixture(autouse=True)
def quick_backoff(monkeypatch):
    import meertex.sync as sync_mod

    monkeypatch.setattr(sync_mod, "BACKOFF_MAX", 0.5)
    monkeypatch.setattr(sync_mod, "STEP2_GRACE", 0.5)


def test_once_writes_the_project_into_an_empty_folder(tmp_path):
    async def main():
        server = FakeServer(free_port())
        y.put_text(server.doc, "main.tex", "\\documentclass{article}\nGrüße 😀\n")
        y.put_text(server.doc, "sections/intro.tex", "Intro\n")
        await server.start()
        try:
            root = make_checkout(tmp_path / "co", server)
            s = session(root, FakeBlobs())
            assert await s.run_once()
            assert (root / "main.tex").read_text() == "\\documentclass{article}\nGrüße 😀\n"
            assert (root / "sections/intro.tex").read_text() == "Intro\n"
            status = json.loads((root / ".meertex/status.json").read_text())
            assert status["connected"] is False and status["last_sync_at"] and not status["unsent"]
        finally:
            await server.stop()

    asyncio.run(main())


def test_once_sends_edits_made_offline(tmp_path):
    async def main():
        server = FakeServer(free_port())
        root = make_checkout(tmp_path / "co", server)
        (root / "main.tex").write_text("written while offline\n")
        s = session(root, FakeBlobs())
        assert not await s.run_once()  # nobody listens
        assert "Cannot reach" in s.last_error
        assert (root / ".meertex/state.bin").exists()
        await server.start()
        try:
            s2 = session(root, FakeBlobs())
            assert await s2.run_once()
            assert server.text("main.tex") == "written while offline\n"
        finally:
            await server.stop()

    asyncio.run(main())


def test_once_reports_a_refused_token(tmp_path):
    async def main():
        server = FakeServer(free_port())
        await server.start()
        try:
            root = make_checkout(tmp_path / "co", server)
            s = session(root, FakeBlobs(), token="wrong")
            assert not await s.run_once()
            assert "4001" in s.last_error and s.fatal
        finally:
            await server.stop()

    asyncio.run(main())


def test_two_live_checkouts_converge(tmp_path):
    async def main():
        server = FakeServer(free_port())
        y.put_text(server.doc, "main.tex", "We measure the energy of LLM inference.\n")
        await server.start()
        a_root = make_checkout(tmp_path / "a", server)
        b_root = make_checkout(tmp_path / "b", server)
        a, b = session(a_root, FakeBlobs()), session(b_root, FakeBlobs())
        stop = asyncio.Event()
        tasks = [asyncio.create_task(a.run_forever(stop)), asyncio.create_task(b.run_forever(stop))]
        try:
            await until(lambda: a.connected and b.connected)
            await until(lambda: (a_root / "main.tex").exists() and (b_root / "main.tex").exists())
            (a_root / "main.tex").write_text("We carefully measure the energy of LLM inference.\n")
            (b_root / "main.tex").write_text("We measure the energy of LLM inference on GPUs.\n")
            merged = "We carefully measure the energy of LLM inference on GPUs.\n"
            await until(lambda: (a_root / "main.tex").read_text() == merged
                        and (b_root / "main.tex").read_text() == merged)
            assert server.text("main.tex") == merged
            # A new file on one side shows up on the other.
            (a_root / "refs.bib").write_text("@misc{x}\n")
            await until(lambda: (b_root / "refs.bib").exists())
        finally:
            stop.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 20)
            await server.stop()

    asyncio.run(main())


def test_offline_edits_sync_after_reconnect(tmp_path):
    async def main():
        server = FakeServer(free_port())
        y.put_text(server.doc, "main.tex", "first\n")
        await server.start()
        root = make_checkout(tmp_path / "co", server)
        s = session(root, FakeBlobs())
        stop = asyncio.Event()
        task = asyncio.create_task(s.run_forever(stop))
        try:
            await until(lambda: s.connected and (root / "main.tex").exists())
            await server.stop()
            await until(lambda: not s.connected)
            (root / "main.tex").write_text("first\nsecond, offline\n")
            await until(lambda: s.unsent)  # in the document, not at the server
            y.put_text(server.doc, "other.tex", "made on the server meanwhile\n")
            await server.start()
            await until(lambda: server.text("main.tex") == "first\nsecond, offline\n", timeout=15)
            await until(lambda: (root / "other.tex").exists())
            await until(lambda: not s.unsent)
        finally:
            stop.set()
            await asyncio.wait_for(task, 20)
            await server.stop()

    asyncio.run(main())


def test_once_asks_the_running_service(tmp_path):
    async def main():
        server = FakeServer(free_port())
        await server.start()
        root = make_checkout(tmp_path / "co", server)
        s = session(root, FakeBlobs())
        lock = CheckoutLock(root)
        assert lock.acquire()
        stop = asyncio.Event()
        task = asyncio.create_task(s.run_forever(stop))
        try:
            await until(lambda: s.connected)
            (root / "main.tex").write_text("from Claude\n")
            assert CheckoutLock.holder(root) is not None
            result = await sync_once(root, TOKEN, Log(quiet=True))
            assert result["ok"] and result["via_service"]
            assert server.text("main.tex") == "from Claude\n"
        finally:
            stop.set()
            await asyncio.wait_for(task, 20)
            lock.release()
            await server.stop()

    asyncio.run(main())


def test_blobs_travel_through_the_store(tmp_path):
    async def main():
        server = FakeServer(free_port())
        await server.start()
        blobs = FakeBlobs()
        a_root = make_checkout(tmp_path / "a", server)
        b_root = make_checkout(tmp_path / "b", server)
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
        (a_root / "fig.png").write_bytes(png)
        try:
            assert await session(a_root, blobs).run_once()
            assert y.materialize(server.doc)["fig.png"].kind == "blob"
            assert await session(b_root, blobs).run_once()
            assert (b_root / "fig.png").read_bytes() == png
        finally:
            await server.stop()

    asyncio.run(main())


def test_service_picks_up_new_checkouts(tmp_path, monkeypatch):
    from meertex.sync import run_daemon

    monkeypatch.setenv("MEERTEX_TOKEN", TOKEN)

    async def main():
        server = FakeServer(free_port())
        y.put_text(server.doc, "main.tex", "hello\n")
        await server.start()
        stop = asyncio.Event()
        daemon = asyncio.create_task(run_daemon(None, Log(quiet=True), stop=stop, poll=0.1))
        try:
            await asyncio.sleep(0.3)  # running, with nothing registered yet
            root = make_checkout(tmp_path / "late", server)
            config.register_checkout(root, config.read_project(root))
            await until(lambda: (root / "main.tex").exists())
            await until(lambda: CheckoutLock.holder(root) is not None)
            (root / "main.tex").write_text("hello again\n")
            await until(lambda: server.text("main.tex") == "hello again\n")
        finally:
            stop.set()
            assert await asyncio.wait_for(daemon, 30) == 0
            await server.stop()
        assert CheckoutLock.holder(root) is None

    asyncio.run(main())


def test_service_follows_hold_and_release(tmp_path):
    async def main():
        server = FakeServer(free_port())
        y.put_text(server.doc, "main.tex", "one\n")
        await server.start()
        root = make_checkout(tmp_path / "co", server)
        s = session(root, FakeBlobs())
        stop = asyncio.Event()
        task = asyncio.create_task(s.run_forever(stop))
        try:
            await until(lambda: s.connected and (root / "main.tex").exists())
            config.set_hold(root)
            await until(lambda: s.engine.held, timeout=2)
            (root / "main.tex").write_text("one\nheld back\n")
            await until(lambda: s.engine.held_paths == {"main.tex"}, timeout=3)
            await until(lambda: json.loads((root / ".meertex/status.json").read_text()).get("held_files") == 1,
                        timeout=3)
            await server.put_text("other.tex", "from a coauthor\n")
            await until(lambda: (root / "other.tex").exists())
            await asyncio.sleep(0.5)
            assert server.text("main.tex") == "one\n" and not s.unsent
            config.clear_hold(root)
            await until(lambda: server.text("main.tex") == "one\nheld back\n", timeout=3)
            assert not s.engine.held and s.engine.held_paths == set()
        finally:
            stop.set()
            await asyncio.wait_for(task, 20)
            await server.stop()

    asyncio.run(main())
