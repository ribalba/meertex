"""A paper whose main file is Markdown, compiled through the server
(docs/DESIGN.md §14 Markdown): problems and SyncTeX name the Markdown file."""

import pytest
from compile_helpers import TOKEN, compile_service, needs_pandoc, needs_tex
from helpers import needs_db, running_app, sign_in
from test_live_helpers import make_project

pytestmark = [needs_db, needs_tex, needs_pandoc]

NOTES = b"""# Notes

A first line,
a second line.

- An item
- Another item
"""


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    with compile_service(tmp_path_factory.mktemp("compile-markdown-api")) as svc:
        yield svc


@pytest.fixture(autouse=True)
def compiling_on(service, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "compile_url", service.url)
    monkeypatch.setattr(get_settings(), "compile_token", TOKEN)


def test_a_markdown_paper(service):
    with running_app() as client:
        sign_in(client)
        me = client.get("/api/auth/me").json()
        pid, _name = make_project(me["id"])
        assert client.patch(f"/api/projects/{pid}", json={"main_file": "notes.md"}).json()["main_file"] == "notes.md"
        assert client.put(f"/api/projects/{pid}/files/notes.md", content=NOTES).status_code == 200

        for editor in (False, True):
            r = client.post(f"/api/projects/{pid}/compile", json={"editor": editor})
            assert r.status_code == 200, r.text
            c = r.json()
            assert (c["status"], c["main_file"], c["problems"]) == ("success", "notes.md", [])
            assert client.get(c["pdf_url"]).content.startswith(b"%PDF-")

            base = f"/api/projects/{pid}/compiles/{c['id']}/synctex"
            boxes = client.post(f"{base}/view", json={"path": "notes.md", "line": 7}).json()["boxes"]
            assert boxes
            point = {"page": 1, "x": boxes[0]["x"] + 3, "y": boxes[0]["y"] + boxes[0]["height"] / 2}
            assert client.post(f"{base}/edit", json=point).json() == {"path": "notes.md", "line": 7, "column": 0}

        broken = NOTES.replace(b"a second line.", b"a second `\\undefinedmacro`{=latex} line.")
        client.put(f"/api/projects/{pid}/files/notes.md", content=broken)
        c = client.post(f"/api/projects/{pid}/compile", json={"editor": True}).json()
        assert c["status"] == "errors"
        errors = [p for p in c["problems"] if p["level"] == "error"]
        assert errors and errors[0]["path"] == "notes.md" and errors[0]["line"] == 4
        assert "Undefined control sequence" in errors[0]["message"]
