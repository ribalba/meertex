"""git clone of a paper's history (docs/DESIGN.md §9, Cloning).

The real git command talks to the app on a real port, in both wire protocols.
The rest goes through the TestClient.
"""

import base64
import gzip
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest
from helpers import api_token, needs_db, running_app, sign_in
from test_live_helpers import bare_client, make_project, make_user

pytestmark = needs_db

PIXELS = bytes(range(256)) * 8


@contextmanager
def http_server():
    """The app on a free port, beside running_app(), which runs the lifespan
    (the rooms are the same ones)."""
    import uvicorn

    from app.main import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def git(*args: str, cwd=None, check: bool = True) -> subprocess.CompletedProcess:
    """The git command without the developer's configuration, and without
    ever asking for a password."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False)
    if check:
        assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r


def basic(token: str, user: str = "didi@example.com") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}


def pkt(line: str) -> bytes:
    return f"{len(line) + 4:04x}".encode() + line.encode()


LS_REFS = pkt("command=ls-refs\n") + b"0001" + b"0000"


@pytest.mark.parametrize("protocol", ["0", "2"])
def test_clone_has_every_snapshot_and_the_version_tags(protocol, tmp_path):
    from app import live
    from app.live import rooms

    with running_app() as client, http_server() as host:
        sign_in(client)
        token = api_token(client)
        p = client.post("/api/projects", json={"name": "clone-me", "starter": "basic"}).json()
        base = f"/api/projects/{p['id']}"
        client.put(f"{base}/files/main.tex", content=b"hello\n")
        assert client.post(f"{base}/versions", json={"kind": "submission"}).status_code == 201
        # Not in a snapshot yet. The clone takes one first.
        client.put(f"{base}/files/fig.png", content=PIXELS)
        assert rooms.loaded(p["id"]).dirty_since is not None

        dest = tmp_path / "clone"
        r = git("-c", f"protocol.version={protocol}", "clone", f"http://didi:{token}@{host}/p/clone-me.git", str(dest))
        assert "warning" not in r.stderr, r.stderr
        assert (dest / "main.tex").read_bytes() == b"hello\n"
        assert (dest / "fig.png").read_bytes() == PIXELS
        assert (dest / "refs.bib").exists()
        assert git("tag", cwd=dest).stdout.split() == ["submission"]
        assert git("symbolic-ref", "HEAD", cwd=dest).stdout.strip() == "refs/heads/main"

        history = client.get(f"{base}/history").json()
        assert git("rev-parse", "HEAD", cwd=dest).stdout.strip() == history[0]["commit"]
        assert git("log", "--format=%H", cwd=dest).stdout.split() == [c["commit"] for c in history]
        assert history[0]["message"] == "Edits by didi@example.com"
        assert live.resolve_commit(p["id"], "submission") == history[1]["commit"]


def test_fetch_brings_new_snapshots_and_push_is_refused(tmp_path):
    from app import live

    with running_app() as client, http_server() as host:
        sign_in(client)
        token = api_token(client)
        p = client.post("/api/projects", json={"name": "paper", "starter": "basic"}).json()
        base = f"/api/projects/{p['id']}"
        dest = tmp_path / "paper"
        git("clone", f"http://x:{token}@{host}/p/{p['id']}", str(dest))

        for i in range(3):
            client.put(f"{base}/files/main.tex", content=f"round {i}\n".encode())
            live.snapshot(p["id"])
        git("pull", "--ff-only", cwd=dest)
        assert (dest / "main.tex").read_text() == "round 2\n"

        (dest / "main.tex").write_text("pushed\n")
        git("commit", "-qam", "from outside", cwd=dest)
        head = live.snapshot(p["id"])
        r = git("push", "origin", "HEAD:main", cwd=dest, check=False)
        assert r.returncode != 0
        assert "meertex does not take pushes" in r.stderr, r.stderr
        assert live.snapshot(p["id"]) == head
        assert client.get(f"{base}/files/main.tex").content == b"round 2\n"


def test_who_may_clone():
    with running_app() as client:
        me = sign_in(client)
        viewer_id, _viewer, viewer_token = make_user()
        _stranger_id, _stranger, stranger_token = make_user()
        pid, name = make_project(me["id"], {viewer_id: "viewer"})
        refs = f"/p/{name}.git/info/refs?service=git-upload-pack"
        bare = bare_client()

        # No token: 401 and git asks for one, whether the paper exists or not.
        for url in (refs, "/p/no-such-paper.git/info/refs?service=git-upload-pack"):
            r = bare.get(url)
            assert r.status_code == 401
            assert r.headers["www-authenticate"].startswith('Basic realm="meertex"')
            assert r.headers["content-type"].startswith("text/plain")
            assert "API token" in r.text
        assert bare.get(refs, headers=basic("wrong")).status_code == 401
        assert bare.get(refs, headers={"Authorization": "Basic %%%"}).status_code == 401
        assert bare.post(f"/p/{name}.git/git-upload-pack", content=LS_REFS).status_code == 401

        # Someone else's paper and no paper at all look the same.
        a = bare.get(refs, headers=basic(stranger_token))
        b = bare.get("/p/no-such-paper.git/info/refs?service=git-upload-pack", headers=basic(stranger_token))
        assert (a.status_code, a.text) == (b.status_code, b.text) == (404, "Repository not found.\n")

        for url in (refs, f"/p/{name}/info/refs?service=git-upload-pack", f"/p/{pid}.git/info/refs?service=git-upload-pack"):
            r = bare.get(url, headers=basic(viewer_token))
            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == "application/x-git-upload-pack-advertisement"
            assert r.headers["cache-control"].startswith("no-cache")
            assert r.content.startswith(b"001e# service=git-upload-pack\n0000")
            assert b"refs/heads/main" in r.content
        r = bare.get(refs, headers={"Authorization": f"Bearer {viewer_token}", "Git-Protocol": "version=2"})
        assert r.status_code == 200
        assert r.content.startswith(pkt("version 2\n"))
        # A header git would not send is left out, and the answer is protocol v0.
        r = bare.get(refs, headers={**basic(viewer_token), "Git-Protocol": "version=2 x"})
        assert r.content.startswith(b"001e# service=git-upload-pack\n0000")


def test_push_dumb_http_and_the_browser():
    with running_app() as client:
        me = sign_in(client)
        _pid, name = make_project(me["id"])
        bare = bare_client()

        # A push is refused before any password, with git's ERR line.
        r = bare.get(f"/p/{name}.git/info/refs?service=git-receive-pack")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-git-receive-pack-advertisement"
        assert r.content[4:].startswith(b"ERR meertex does not take pushes.")
        assert f"(meertex checkout {name}).\n".encode() in r.content
        assert int(r.content[:4], 16) == len(r.content)
        r = bare.get("/p/a%0Ab.git/info/refs?service=git-receive-pack")
        assert b"(meertex checkout <name>)" in r.content
        assert bare.post(f"/p/{name}.git/git-receive-pack", content=b"0000").status_code in (404, 405)

        assert bare.get(f"/p/{name}.git/info/refs").status_code == 403
        r = bare.get(f"/p/{name}.git", follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (302, f"/p/{name}")


def test_upload_pack_requests(monkeypatch):
    from app.live import rooms
    from app.routers import git as git_router

    with running_app() as client:
        me = sign_in(client)
        token = api_token(client)
        pid, name = make_project(me["id"], template="basic")
        rooms.drop(pid)
        url = f"/p/{name}.git/git-upload-pack"
        v2 = {**basic(token), "Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request"}
        bare = bare_client()

        # git gzips a request above 1 KB. The pack goes out as it is.
        for body, headers in ((LS_REFS, v2), (gzip.compress(LS_REFS), {**v2, "Content-Encoding": "gzip"})):
            r = bare.post(url, content=body, headers={**headers, "Accept-Encoding": "gzip"})
            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == "application/x-git-upload-pack-result"
            assert "content-encoding" not in r.headers
            assert b"refs/heads/main" in r.content
        # A clone of a paper nobody has open does not load it.
        assert bare.get(f"/p/{name}.git/info/refs?service=git-upload-pack", headers=basic(token)).status_code == 200
        assert rooms.loaded(pid) is None

        r = bare.post(url, content=b"not gzip", headers={**v2, "Content-Encoding": "gzip"})
        assert r.status_code == 400
        monkeypatch.setattr(git_router, "MAX_REQUEST_BYTES", 1000)
        assert bare.post(url, content=b"0" * 1001, headers=v2).status_code == 413
        r = bare.post(url, content=gzip.compress(b"0" * 5000), headers={**v2, "Content-Encoding": "gzip"})
        assert r.status_code == 413
