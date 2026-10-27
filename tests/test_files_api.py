"""Files, zips, diffs and blobs over HTTP (docs/DESIGN.md §11)."""

import hashlib
import io
import zipfile

from helpers import needs_db, running_app, sign_in
from test_live_helpers import bare_client, make_project, make_user

pytestmark = needs_db

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
INTRO = "Grüße aus Golm 😀\nZweite Zeile\n"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_text_and_blob_files_with_umlauts():
    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        base = f"/api/projects/{pid}/files"

        r = client.put(f"{base}/sections/Einführung.tex", content=INTRO.encode())
        assert r.status_code == 200, r.text
        intro = r.json()
        assert intro["path"] == "sections/Einführung.tex"
        assert intro["kind"] == "text"
        assert intro["size"] == len(INTRO.encode())
        assert intro["sha"] == hashlib.sha256(INTRO.encode()).hexdigest()

        r = client.get(f"{base}/sections/Einführung.tex")
        assert r.status_code == 200
        assert r.content.decode() == INTRO
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"] == "sandbox"
        assert "filename*=UTF-8''Einf%C3%BChrung.tex" in r.headers["content-disposition"]
        assert r.headers["content-disposition"].startswith("inline")

        r = client.put(f"{base}/figures/plot.png", content=PNG)
        assert r.json()["kind"] == "blob"
        assert r.json()["sha"] == hashlib.sha256(PNG).hexdigest()
        r = client.get(f"{base}/figures/plot.png")
        assert r.content == PNG
        assert r.headers["content-type"] == "image/png"
        assert r.headers["content-disposition"].startswith("inline")
        assert r.headers["content-security-policy"] == "sandbox"

        # HTML and SVG are never shown: as text they are plain text, as a
        # blob they are a download.
        client.put(f"{base}/page.html", content=b"<script>alert(1)</script>")
        r = client.get(f"{base}/page.html")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        client.put(f"{base}/evil.svg", content=b"<svg>\x00<script>alert(1)</script></svg>")
        r = client.get(f"{base}/evil.svg")
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["content-disposition"] == 'attachment; filename="evil.svg"'
        assert r.headers["x-content-type-options"] == "nosniff"

        listed = client.get(base).json()
        assert [f["path"] for f in listed] == ["evil.svg", "figures/plot.png", "page.html", "sections/Einführung.tex"]
        assert all(f["id"] for f in listed)

        # A blob becomes text and back, under the same path.
        r = client.put(f"{base}/figures/plot.png", content=b"now text")
        assert r.json()["kind"] == "text"

        assert client.delete(f"{base}/evil.svg").json() == {"deleted": ["evil.svg"]}
        assert client.get(f"{base}/evil.svg").status_code == 404
        assert client.delete(f"{base}/evil.svg").status_code == 404


def test_moves_keep_ids_and_refuse_clashes():
    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        base = f"/api/projects/{pid}/files"
        first = client.put(f"{base}/sections/Einführung.tex", content=INTRO.encode()).json()
        client.put(f"{base}/sections/related.tex", content=b"related")
        client.put(f"{base}/main.tex", content=b"main")

        r = client.post(f"{base}/move", json={"from": "sections/Einführung.tex", "to": "intro.tex"})
        assert r.status_code == 200, r.text
        assert r.json() == [{**first, "path": "intro.tex"}]
        assert client.get(f"{base}/sections/Einführung.tex").status_code == 404
        assert client.get(f"{base}/intro.tex").text == INTRO

        # A folder moves with everything in it.
        r = client.post(f"{base}/move", json={"from": "sections", "to": "kapitel"})
        assert [f["path"] for f in r.json()] == ["kapitel/related.tex"]

        assert client.post(f"{base}/move", json={"from": "nope.tex", "to": "x.tex"}).status_code == 404
        r = client.post(f"{base}/move", json={"from": "intro.tex", "to": "main.tex"})
        assert r.status_code == 422
        assert "exists" in r.json()["detail"]
        assert client.post(f"{base}/move", json={"from": "intro.tex", "to": "../x.tex"}).status_code == 422

        # A path cannot be a file and a folder at once.
        assert client.put(f"{base}/kapitel", content=b"x").status_code == 422
        assert client.put(f"{base}/main.tex/x.tex", content=b"x").status_code == 422
        assert client.put(f"{base}/.meertex/state.bin", content=b"x").status_code == 422

        # A folder goes with everything in it.
        assert client.delete(f"{base}/kapitel").json() == {"deleted": ["kapitel/related.tex"]}
        assert [f["path"] for f in client.get(base).json()] == ["intro.tex", "main.tex"]


def test_roles_and_limits(monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        me = sign_in(client)
        viewer_id, _viewer, viewer_token = make_user()
        _stranger_id, _stranger, stranger_token = make_user()
        pid, _name = make_project(me["id"], {viewer_id: "viewer"})
        base = f"/api/projects/{pid}/files"
        client.put(f"{base}/main.tex", content=b"hello")

        bare = bare_client()
        assert bare.get(base).status_code == 401
        assert bare.get(f"{base}/main.tex", headers=_bearer(viewer_token)).text == "hello"
        assert bare.get(f"{base}/main.tex?token={viewer_token}").text == "hello"
        r = bare.put(f"{base}/main.tex", content=b"no", headers=_bearer(viewer_token))
        assert r.status_code == 403
        assert bare.delete(f"{base}/main.tex", headers=_bearer(viewer_token)).status_code == 403
        assert bare.get(base, headers=_bearer(stranger_token)).status_code == 404

        monkeypatch.setattr(get_settings(), "max_blob_bytes", 1000)
        assert client.put(f"{base}/big.bin", content=bytes(1001)).status_code == 413
        assert client.put(f"{base}/small.bin", content=bytes(1000)).status_code == 200


def test_refs_tags_and_zips():
    from app import live

    with running_app() as client:
        me = sign_in(client)
        pid, name = make_project(me["id"])
        base = f"/api/projects/{pid}"
        client.put(f"{base}/files/sections/Einführung.tex", content=INTRO.encode())
        client.put(f"{base}/files/figures/plot.png", content=PNG)
        live.create_tag(pid, "submission", live.snapshot(pid))
        client.put(f"{base}/files/main.tex", content=b"after the submission")

        tagged = client.get(f"{base}/files?ref=submission").json()
        assert [f["path"] for f in tagged] == ["figures/plot.png", "sections/Einführung.tex"]
        assert [f["kind"] for f in tagged] == ["blob", "text"]
        assert all(f["id"] is None for f in tagged)
        assert client.get(f"{base}/files/sections/Einführung.tex?ref=submission").text == INTRO
        r = client.get(f"{base}/files/figures/plot.png?ref=submission")
        assert r.content == PNG
        assert r.headers["content-type"] == "image/png"
        assert client.get(f"{base}/files/main.tex?ref=submission").status_code == 404
        assert client.get(f"{base}/files/main.tex?ref=HEAD").status_code == 404
        assert client.get(f"{base}/files/main.tex").text == "after the submission"

        for url in (
            f"{base}/files?ref=--output=x",
            f"{base}/files/main.tex?ref=--output=x",
            f"{base}/zip?ref=--output=x",
            f"{base}/diff?from=--output=x",
            f"{base}/diff?from=HEAD&to=-x",
        ):
            r = client.get(url)
            assert r.status_code == 422, url
        for url in (f"{base}/files?ref=nope", f"{base}/zip?ref=nope", f"{base}/diff?from=nope"):
            assert client.get(url).status_code == 404, url

        r = client.get(f"{base}/zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert f'filename="{name}-live.zip"' in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert sorted(zf.namelist()) == ["figures/plot.png", "main.tex", "sections/Einführung.tex"]
            assert zf.read("sections/Einführung.tex").decode() == INTRO
            assert zf.read("figures/plot.png") == PNG

        r = client.get(f"{base}/zip?ref=submission")
        assert f'filename="{name}-submission.zip"' in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            # git archive lists the folders too.
            assert sorted(n for n in zf.namelist() if not n.endswith("/")) == ["figures/plot.png", "sections/Einführung.tex"]
            assert zf.read("figures/plot.png") == PNG


def test_diff_with_renames_and_words():
    from app import live

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        base = f"/api/projects/{pid}"
        paper = "We measure the energy of LLM inference.\n" + "".join(f"Line {i}\n" for i in range(10))
        client.put(f"{base}/files/main.tex", content=paper.encode())
        client.put(f"{base}/files/old name.tex", content=b"".join(b"Same line %d\n" % i for i in range(10)))
        client.put(f"{base}/files/figures/plot.png", content=PNG)
        submitted = live.snapshot(pid)
        live.create_tag(pid, "submission", submitted)

        client.put(f"{base}/files/main.tex", content=paper.replace("the energy", "the carbon").encode())
        client.post(f"{base}/files/move", json={"from": "old name.tex", "to": "neuer Name.tex"})
        client.put(f"{base}/files/figures/plot.png", content=PNG + b"\x00more")
        client.put(f"{base}/files/added.tex", content=b"new\n")

        r = client.get(f"{base}/diff?from=submission")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["from"] == submitted
        assert body["to"] == live.resolve_commit(pid, "HEAD")  # live was snapshotted
        files = {f["path"]: f for f in body["files"]}
        assert files["added.tex"]["status"] == "added"
        assert files["neuer Name.tex"]["status"] == "renamed"
        assert files["neuer Name.tex"]["old_path"] == "old name.tex"
        assert files["figures/plot.png"] == {
            "path": "figures/plot.png",
            "old_path": None,
            "status": "modified",
            "binary": True,
            "diff": "",
        }
        main = files["main.tex"]
        assert main["status"] == "modified"
        assert "-We measure the energy of LLM inference." in main["diff"]
        assert "+We measure the carbon of LLM inference." in main["diff"]
        assert "Line 3" not in main["diff"]  # three lines of context, no more

        words = {f["path"]: f for f in client.get(f"{base}/diff?from=submission&to=HEAD&word=true").json()["files"]}
        lines = words["main.tex"]["diff"].splitlines()
        assert "-energy" in lines
        assert "+carbon" in lines
        assert "~" in lines

        r = client.get(f"{base}/diff?from=HEAD&to=HEAD")
        assert r.json()["files"] == []


def test_blobs_by_hash():
    with running_app() as client:
        me = sign_in(client)
        viewer_id, _viewer, viewer_token = make_user()
        pid, _name = make_project(me["id"], {viewer_id: "viewer"})
        base = f"/api/projects/{pid}/blobs"
        sha = hashlib.sha256(PNG).hexdigest()

        r = client.put(f"{base}/{'0' * 64}", content=PNG)
        assert r.status_code == 422
        assert sha in r.json()["detail"]
        assert client.put(f"{base}/not-a-hash", content=PNG).status_code == 422
        assert client.get(f"{base}/{sha}").status_code == 404

        r = client.put(f"{base}/{sha}", content=PNG)
        assert r.status_code == 200, r.text
        assert r.json() == {"sha": sha, "size": len(PNG)}

        bare = bare_client()
        r = bare.get(f"{base}/{sha}", headers=_bearer(viewer_token))
        assert r.content == PNG
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["content-disposition"].startswith("attachment")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert bare.put(f"{base}/{sha}", content=PNG, headers=_bearer(viewer_token)).status_code == 403


def test_if_none_match_only_creates():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "create-only"}).json()
        url = f"/api/projects/{p['id']}/files/intro.tex"
        first = client.put(url, content=b"mine", headers={"If-None-Match": "*"})
        assert first.status_code == 200, first.text
        second = client.put(url, content=b"theirs", headers={"If-None-Match": "*"})
        assert second.status_code == 412
        assert client.get(url).text == "mine"
        # Without the header a PUT writes as before.
        assert client.put(url, content=b"theirs").status_code == 200
        assert client.get(url).text == "theirs"


def test_static_files_are_compressed():
    with running_app() as client:
        r = client.get("/static/vendor/editor/editor.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"
