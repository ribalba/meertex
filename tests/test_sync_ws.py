"""The sync endpoint (docs/DESIGN.md §10) and the rooms behind it (§8, §9)."""

import json

import pytest
from helpers import needs_db, running_app, sign_in
from pycrdt import Doc, create_awareness_message, write_var_uint
from test_live_helpers import Peer, bare_client, make_project, make_user, wait_for

pytestmark = needs_db

PARAGRAPH = "We measure the energy of LLM inference."


def _url(pid: str, token: str | None = None) -> str:
    return f"/api/projects/{pid}/sync" + (f"?token={token}" if token else "")


def _text(doc: Doc, path: str) -> str | None:
    from app.live import ydoc

    entry = ydoc.find(doc, path)
    return entry.text if entry is not None else None


def _write(path: str, text: str):
    from app.live import ydoc

    def change(doc: Doc) -> None:
        entry = ydoc.find(doc, path)
        if entry is None:
            ydoc.add_text(doc, path, text)
        else:
            ydoc.write_text(doc, entry.id, text)

    return change


def _server_text(pid: str, path: str) -> str | None:
    from app.live import rooms

    with rooms.get(pid).read() as doc:
        return _text(doc, path)


def _put(pid: str, path: str, text: str, editor: str = "test@example.com") -> None:
    from app import live

    live.write_file(pid, path, text.encode(), editor)


def _commit_messages(pid: str) -> list[str]:
    from app.live import git

    return git.run(pid, "log", "--format=%s").decode().splitlines()


def test_two_clients_editing_one_paragraph_converge():
    with running_app() as client:
        me = sign_in(client)
        bob_id, _bob, bob_token = make_user()
        pid, _name = make_project(me["id"], {bob_id: "editor"})
        _put(pid, "main.tex", PARAGRAPH)

        bare = bare_client()
        with client.websocket_connect(_url(pid)) as a_ws, bare.websocket_connect(_url(pid, bob_token)) as b_ws:
            a, b = Peer(a_ws), Peer(b_ws)
            a.sync()
            b.sync()
            assert _text(a.doc, "main.tex") == _text(b.doc, "main.tex") == PARAGRAPH

            # Both edit before either has seen the other's change.
            update_a = a.edit(_write("main.tex", "We carefully measure the energy of LLM inference."))
            update_b = b.edit(_write("main.tex", "We measure the energy of LLM inference on GPUs."))
            a_ws.send_bytes(update_a)
            b_ws.send_bytes(update_b)
            a.sync()
            b.sync()
            a.drain()
            b.drain()

            merged = "We carefully measure the energy of LLM inference on GPUs."
            assert _text(a.doc, "main.tex") == merged
            assert _text(b.doc, "main.tex") == merged
            assert _server_text(pid, "main.tex") == merged


def test_api_put_reaches_a_connected_client_and_keeps_its_concurrent_edit():
    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        _put(pid, "main.tex", PARAGRAPH)

        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()

            # An edit the server has not seen yet, while the API writes the
            # file from a copy that does not have it either.
            pending = peer.edit(_write("main.tex", "We carefully measure the energy of LLM inference."))
            r = client.put(f"/api/projects/{pid}/files/main.tex", content=b"We measure the energy of LLM inference on GPUs.")
            assert r.status_code == 200, r.text
            ws.send_bytes(pending)
            peer.sync()
            peer.drain()

            merged = "We carefully measure the energy of LLM inference on GPUs."
            assert _text(peer.doc, "main.tex") == merged
            assert _server_text(pid, "main.tex") == merged

            # A new file from the API arrives too, umlauts and all.
            r = client.put(f"/api/projects/{pid}/files/Einführung.tex", content="Grüße aus Golm 😀".encode())
            assert r.status_code == 200, r.text
            assert wait_for(lambda: (peer.drain(0.1), _text(peer.doc, "Einführung.tex"))[1] == "Grüße aus Golm 😀")


def test_viewer_updates_are_dropped_but_viewers_get_everything():
    with running_app() as client:
        me = sign_in(client)
        viewer_id, _viewer, viewer_token = make_user()
        pid, _name = make_project(me["id"], {viewer_id: "viewer"})
        _put(pid, "main.tex", PARAGRAPH)

        bare = bare_client()
        with client.websocket_connect(_url(pid)) as a_ws, bare.websocket_connect(_url(pid, viewer_token)) as v_ws:
            a, v = Peer(a_ws), Peer(v_ws)
            a.sync()
            v.sync()
            v.push(_write("main.tex", "The viewer wrote this."))
            v.sync()
            a.drain()
            assert _server_text(pid, "main.tex") == PARAGRAPH
            assert _text(a.doc, "main.tex") == PARAGRAPH

            a.push(_write("main.tex", PARAGRAPH + " Editors write."))
            a.sync()
            assert wait_for(lambda: (v.drain(0.1), "Editors write." in (_text(v.doc, "main.tex") or ""))[1])


def test_sign_in_and_membership_close_codes():
    with running_app() as client:
        me = sign_in(client)
        pid, name = make_project(me["id"])
        stranger_id, _stranger, stranger_token = make_user()
        _other_pid, _ = make_project(stranger_id)

        anonymous = bare_client()
        with anonymous.websocket_connect(_url(pid)) as ws:
            assert Peer(ws).close_code() == 4001
        with anonymous.websocket_connect(_url(pid, "not-a-token")) as ws:
            assert Peer(ws).close_code() == 4001
        with anonymous.websocket_connect(_url(pid, stranger_token)) as ws:
            assert Peer(ws).close_code() == 4004
        with anonymous.websocket_connect(_url("no-such-project", stranger_token)) as ws:
            assert Peer(ws).close_code() == 4004

        # The token as a bearer header, and the project by its name.
        with client.websocket_connect(f"/api/projects/{name}/sync") as ws:
            Peer(ws).sync()
        headers = {"Authorization": f"Bearer {stranger_token}"}
        with anonymous.websocket_connect(f"/api/projects/{_other_pid}/sync", headers=headers) as ws:
            Peer(ws).sync()

        # The session cookie does not count from a foreign page.
        with client.websocket_connect(_url(pid), headers={"Origin": "https://evil.example"}) as ws:
            assert Peer(ws).close_code() == 4001
        with client.websocket_connect(_url(pid), headers={"Origin": "http://testserver"}) as ws:
            Peer(ws).sync()


@pytest.mark.parametrize(
    "message",
    [
        b"\x07",  # an unknown message type
        b"\x00\x00\x05ab",  # a payload that is cut off
        b"\x00\x02\x03xyz",  # an update that is not one
        b"\x00\x00\x01\x00junk",  # bytes after the payload
        b"\x01\x05\x01\x02\x03\x04\x05",  # an awareness update that is not one
        "text",  # a text frame
        b"\x00\x02" + write_var_uint(200) + bytes(200),  # larger than the limit below
    ],
)
def test_invalid_messages_close_with_4400(message, monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        monkeypatch.setattr(get_settings(), "max_sync_message_bytes", 100)
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            if isinstance(message, str):
                ws.send_text(message)
            else:
                ws.send_bytes(message)
            assert peer.close_code() == 4400


def test_updates_are_stored_in_batches_and_survive_dropping_the_room():
    from app import live
    from app.live import rooms, store

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            peer.push(_write("main.tex", "first"))
            peer.sync()
            # Still connected: the batch is written within about half a second.
            assert wait_for(lambda: _text(store.load(pid)[0], "main.tex") == "first")
            peer.push(_write("main.tex", "first and second"))
            peer.sync()
        # Closing writes right away.
        assert _text(store.load(pid)[0], "main.tex") == "first and second"
        rooms.drop(pid)
        assert [f["path"] for f in live.list_files(pid)] == ["main.tex"]
        assert _server_text(pid, "main.tex") == "first and second"


def test_snapshot_timer_commits_with_the_editors(monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        monkeypatch.setattr(get_settings(), "snapshot_idle_seconds", 1)
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            peer.push(_write("main.tex", "snapshot me"))
            peer.sync()
            assert wait_for(lambda: _commit_messages(pid)[0] == f"Edits by {me['email']}", timeout=8)


def test_snapshot_max_seconds_while_edits_keep_coming(monkeypatch):
    import time

    from app.config import get_settings

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        monkeypatch.setattr(get_settings(), "snapshot_max_seconds", 1)
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            deadline = time.monotonic() + 8
            n = 0
            while len(_commit_messages(pid)) < 2 and time.monotonic() < deadline:
                n += 1
                peer.push(_write("main.tex", f"edit {n}"))
                peer.sync()
                time.sleep(0.2)
            assert len(_commit_messages(pid)) >= 2


def test_idle_rooms_are_written_out_and_unloaded(monkeypatch):
    from app.config import get_settings
    from app.live import git, rooms

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            peer.push(_write("main.tex", "written out on unload"))
            peer.sync()
            monkeypatch.setattr(get_settings(), "room_idle_seconds", 1)
            # Connected rooms stay, however long nothing happens.
            assert not wait_for(lambda: pid not in {r.project_id for r in rooms.open_rooms()}, timeout=2)
        assert wait_for(lambda: pid not in {r.project_id for r in rooms.open_rooms()})
        head = git.head(pid)
        assert git.show(pid, head, "main.tex") == b"written out on unload"
        assert _commit_messages(pid)[0] == f"Edits by {me['email']}"


def test_a_room_held_across_an_unload_sends_edits_to_its_successor(monkeypatch):
    from app.config import get_settings
    from app.live import rooms, ydoc

    with running_app():
        owner_id, _email, _token = make_user()
        pid, _name = make_project(owner_id)
        old = rooms.get(pid)
        monkeypatch.setattr(get_settings(), "room_idle_seconds", 0)
        assert rooms.unload(old)
        with old.edit("late@example.com") as doc:
            ydoc.put_text(doc, "late.tex", "not lost")
        assert rooms.get(pid) is not old
        assert _server_text(pid, "late.tex") == "not lost"


def test_deleting_the_project_closes_its_connections():
    from app import live

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            live.delete_project(pid)
            assert peer.close_code() == 4004


def test_a_room_whose_project_row_is_gone_closes_with_4004():
    from app.database import SessionLocal
    from app.live import rooms, store
    from app.models import Project

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        other, _ = make_project(me["id"])
        with client.websocket_connect(_url(pid)) as ws:
            peer = Peer(ws)
            peer.sync()
            # The projects API deletes the rows before it calls live.delete_project().
            with SessionLocal() as db:
                db.query(Project).filter(Project.id == pid).delete()
                db.commit()
            peer.push(_write("main.tex", "too late"))
            assert peer.close_code() == 4004
        assert pid not in {r.project_id for r in rooms.open_rooms()}

        # The background work goes on for the other projects.
        with client.websocket_connect(_url(other)) as ws:
            peer = Peer(ws)
            peer.sync()
            peer.push(_write("main.tex", "still stored"))
            peer.sync()
            assert wait_for(lambda: _text(store.load(other)[0], "main.tex") == "still stored")


def _awareness(client_id: int, clock: int, state: dict | None) -> bytes:
    raw = json.dumps(state).encode()
    update = write_var_uint(1) + write_var_uint(client_id) + write_var_uint(clock) + write_var_uint(len(raw)) + raw
    return create_awareness_message(update)


def _awareness_states(message: bytes) -> list[tuple[int, int, object]]:
    """(client id, clock, state) of an awareness message, decoded the long way."""
    from app.routers.sync import _var_bytes, _var_uint

    kind, pos = _var_uint(message, 0)
    assert kind == 1
    update, _end = _var_bytes(message, pos)
    count, pos = _var_uint(update, 0)
    out = []
    for _ in range(count):
        client_id, pos = _var_uint(update, pos)
        clock, pos = _var_uint(update, pos)
        raw, pos = _var_bytes(update, pos)
        out.append((client_id, clock, json.loads(raw)))
    return out


def test_awareness_is_relayed_remembered_and_removed():
    with running_app() as client:
        me = sign_in(client)
        bob_id, _bob, bob_token = make_user()
        pid, _name = make_project(me["id"], {bob_id: "viewer"})

        bare = bare_client()
        with bare.websocket_connect(_url(pid, bob_token)) as b_ws:
            b = Peer(b_ws)
            b.sync()
            with client.websocket_connect(_url(pid)) as a_ws:
                a = Peer(a_ws)
                a.sync()
                # A claims to be someone else. The server signs the state with
                # who A really is before anybody sees it.
                a_ws.send_bytes(_awareness(4711, 1, {"user": {"name": "Alice", "email": "alice@x", "color": "#123"}, "cursor": None}))
                a.sync()
                b.drain()
                seen = [s for m in b.received if m[0] == 1 for s in _awareness_states(m)]
                assert (4711, 1, {"user": {"name": me["email"], "email": me["email"], "color": "#123"}, "cursor": None}) in seen

                # Someone who connects later sees who is there, signed too.
                with bare.websocket_connect(_url(pid, bob_token)) as c_ws:
                    c = Peer(c_ws)
                    c.sync()
                    later = [s for m in c.received if m[0] == 1 for s in _awareness_states(m)]
                    assert any(state and state["user"]["email"] == me["email"] for _cid, _clock, state in later)
                    assert not any(b"Alice" in m for m in c.received)
            # A left without saying goodbye: the server says it for A.
            b.drain()
            assert b.received[-1] == _awareness(4711, 1, None)


def test_a_connection_that_does_not_read_is_closed(monkeypatch):
    import asyncio

    from app.live import rooms

    monkeypatch.setattr(rooms, "MAX_OUTBOX_BYTES", 1000)

    async def scenario():
        conn = rooms.Connection("didi@example.com", True)
        conn.send(b"x" * 600)
        conn.sent(b"x" * 600)  # written to the socket, so it does not count
        conn.send(b"y" * 600)
        assert not conn.closing
        conn.send(b"z" * 600)  # 1200 bytes waiting: it does not read
        assert conn.closing
        assert conn.outbox.qsize() == 1
        assert conn.outbox.get_nowait() == (rooms.CLOSE_TRY_AGAIN, "Too far behind, connect again")
        conn.send(b"after")  # nothing more after the close
        assert conn.outbox.empty()

    asyncio.run(scenario())


def test_removing_or_re_roling_a_member_closes_their_connections():
    with running_app() as client:
        me = sign_in(client)
        arne_id, _arne_email, arne_token = make_user()
        pid, _name = make_project(me["id"], {arne_id: "editor"})
        anonymous = bare_client()

        # A new role: the connection closes with 4009, and the next one has it.
        with anonymous.websocket_connect(_url(pid, arne_token)) as ws:
            peer = Peer(ws)
            peer.sync()
            r = client.patch(f"/api/projects/{pid}/members/{arne_id}", json={"role": "viewer"})
            assert r.status_code == 200, r.text
            assert peer.close_code() == 4009

        # Removed: 4004, and connecting again is refused the same way.
        with anonymous.websocket_connect(_url(pid, arne_token)) as ws:
            peer = Peer(ws)
            peer.sync()
            assert client.delete(f"/api/projects/{pid}/members/{arne_id}").status_code == 200
            assert peer.close_code() == 4004
        with anonymous.websocket_connect(_url(pid, arne_token)) as ws:
            assert Peer(ws).close_code() == 4004
