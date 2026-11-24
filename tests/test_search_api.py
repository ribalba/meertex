"""The search over a project's files (docs/DESIGN.md §11, Search)."""

from helpers import needs_db, running_app, sign_in
from test_live_helpers import bare_client, make_project, make_user

pytestmark = needs_db

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))


def _paths(result: dict) -> list[str]:
    return [f["path"] for f in result["files"]]


def test_regex_case_and_file_types():
    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        base = f"/api/projects/{pid}"
        client.put(f"{base}/files/main.tex", content=b"\\section{Intro}\nWe measure Energy.\n\\section{Method}\n")
        client.put(f"{base}/files/refs.bib", content=b"@article{energy2024,\n  title={Energy}\n}\n")
        client.put(f"{base}/files/notes.md", content="Grüße 😀 energy\n".encode())
        client.put(f"{base}/files/figures/plot.png", content=PNG)
        search = f"{base}/search"

        r = client.get(search, params={"q": r"\\section\{(\w+)\}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["refs"] == ["live"]
        assert _paths(body) == ["main.tex"]
        lines = body["files"][0]["lines"]
        assert [(x["line"], x["text"]) for x in lines] == [(1, "\\section{Intro}"), (3, "\\section{Method}")]
        assert lines[0]["ranges"] == [[0, 15]]

        # Case is ignored unless asked for.
        assert _paths(client.get(search, params={"q": "energy"}).json()) == ["main.tex", "notes.md", "refs.bib"]
        assert _paths(client.get(search, params={"q": "energy", "case": "true"}).json()) == ["notes.md", "refs.bib"]
        assert _paths(client.get(search, params={"q": "energy", "ext": "tex,.BIB"}).json()) == ["main.tex", "refs.bib"]

        # Ranges are UTF-16, what JavaScript slices: the emoji counts twice.
        notes = client.get(search, params={"q": "energy", "ext": "md"}).json()["files"][0]["lines"][0]
        assert notes["ranges"] == [[9, 15]]
        assert notes["text"][:6] == "Grüße "

        # Plain text takes regex characters as they are.
        assert client.get(search, params={"q": "={energy}", "regex": "false"}).json()["lines"] == 1
        r = client.get(search, params={"q": "{energy[", "regex": "true"})
        assert r.status_code == 422
        assert "regular expression" in r.json()["detail"]

        assert client.get(search, params={"q": ""}).status_code == 422
        assert client.get(search, params={"q": "x", "ref": "nope"}).status_code == 404
        assert client.get(search, params={"q": "x", "ref": "a..b"}).status_code == 422


def test_versions_are_searched_once_per_content():
    from app import live

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        base = f"/api/projects/{pid}"
        client.put(f"{base}/files/main.tex", content=b"draft one\nshared line\n")
        client.put(f"{base}/files/refs.bib", content=b"same everywhere\n")
        sub = client.post(f"{base}/versions", json={"kind": "submission"}).json()
        client.put(f"{base}/files/main.tex", content=b"draft two\nshared line\n")
        cam = client.post(f"{base}/versions", json={"kind": "camera_ready"}).json()
        client.put(f"{base}/files/main.tex", content=b"draft three\n")
        live.snapshot(pid)

        r = client.get(f"{base}/search", params={"q": "draft|same", "versions": "true"}).json()
        assert r["refs"] == [cam["tag"], sub["tag"]]
        assert [(f["path"], f["refs"], f["lines"][0]["text"]) for f in r["files"]] == [
            ("main.tex", [cam["tag"]], "draft two"),
            ("main.tex", [sub["tag"]], "draft one"),
            ("refs.bib", [cam["tag"], sub["tag"]], "same everywhere"),
        ]

        # One version, HEAD and the live document.
        one = client.get(f"{base}/search", params={"q": "draft", "ref": sub["tag"]}).json()
        assert [x["text"] for f in one["files"] for x in f["lines"]] == ["draft one"]
        head = client.get(f"{base}/search", params={"q": "draft", "ref": "HEAD"}).json()
        assert [x["text"] for f in head["files"] for x in f["lines"]] == ["draft three"]
        assert client.get(f"{base}/search", params={"q": "shared"}).json()["files"] == []


def test_long_lines_and_limits(monkeypatch):
    from app import search

    with running_app() as client:
        me = sign_in(client)
        pid, _name = make_project(me["id"])
        base = f"/api/projects/{pid}"
        paragraph = "x" * 1000 + "needle" + "y" * 1000
        client.put(f"{base}/files/long.tex", content=(paragraph + "\n" + "needle\n" * 20).encode())

        r = client.get(f"{base}/search", params={"q": "needle"}).json()
        first = r["files"][0]["lines"][0]
        assert len(first["text"]) == search.SNIPPET
        assert first["cut_before"] and first["cut_after"]
        start, end = first["ranges"][0]
        assert first["text"][start:end] == "needle"
        assert r["lines"] == 21

        monkeypatch.setattr(search, "MAX_LINES", 5)
        r = client.get(f"{base}/search", params={"q": "needle"}).json()
        assert r["truncated"]
        assert r["files"][0]["count"] == 21
        assert len(r["files"][0]["lines"]) == 5

        # A pattern that backtracks forever stops at the time budget.
        monkeypatch.setattr(search, "TIME_BUDGET", 0.3)
        client.put(f"{base}/files/slow.tex", content=("a" * 40 + "b\n").encode())
        r = client.get(f"{base}/search", params={"q": "(a|aa)+$", "ext": "tex"}).json()
        assert r["timed_out"]


def test_who_may_search():
    with running_app() as client:
        me = sign_in(client)
        viewer_id, _viewer, viewer_token = make_user()
        _stranger_id, _stranger, stranger_token = make_user()
        pid, _name = make_project(me["id"], {viewer_id: "viewer"})
        client.put(f"/api/projects/{pid}/files/main.tex", content=b"hello\n")
        bare = bare_client()
        url = f"/api/projects/{pid}/search?q=hello"
        assert bare.get(url, headers={"Authorization": f"Bearer {viewer_token}"}).json()["lines"] == 1
        assert bare.get(url, headers={"Authorization": f"Bearer {stranger_token}"}).status_code == 404
