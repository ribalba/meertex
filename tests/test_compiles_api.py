"""Compiling through the server (docs/DESIGN.md §14): the routes, caching by
hash, one compile per hash at a time, problems, versions and pdfdiff, against
the compile service run natively with this machine's TeX Live."""

import threading
import uuid

import pytest
from compile_helpers import HELLO, INTRO, TOKEN, compile_service, needs_tex
from helpers import needs_db, running_app, second_client, sign_in
from test_live_helpers import make_project, make_user, wait_for

pytestmark = [needs_db, needs_tex]

COMPILE_KEYS = {
    "id", "kind", "status", "ref", "commit", "compiler", "main_file", "duration_ms", "created_at", "problems",
    "pdf_url", "log_url",
}
BROKEN = rb"""\documentclass{article}
\begin{document}
See \ref{nowhere}.

\input{sections/intro}

\hbox to 1cm{An overfull box with far too much text in it}
\end{document}
"""
BROKEN_INTRO = b"""Intro (with a parenthesis that never closes.
\\undefinedmacro here
"""


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    with compile_service(tmp_path_factory.mktemp("compile-api"), COMPILE_KEEP_JOBS=200) as svc:
        yield svc


@pytest.fixture(autouse=True)
def compiling_on(service, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "compile_url", service.url)
    monkeypatch.setattr(get_settings(), "compile_token", TOKEN)


def _project(client, files: dict[str, bytes] | None = None) -> str:
    me = client.get("/api/auth/me").json()
    pid, _name = make_project(me["id"])
    for path, data in (files or {"main.tex": HELLO, "sections/intro.tex": INTRO}).items():
        assert client.put(f"/api/projects/{pid}/files/{path}", content=data).status_code == 200
    return pid


def _compile(client, pid: str, ref: str | None = None) -> dict:
    r = client.post(f"/api/projects/{pid}/compile", json={"ref": ref} if ref else None)
    assert r.status_code == 200, r.text
    return r.json()


def test_compile_live_and_read_it(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        before = service.job_count()

        c = _compile(client, pid)
        assert set(c) == COMPILE_KEYS
        assert c["status"] == "success"
        assert (c["kind"], c["ref"], c["commit"]) == ("pdf", "live", None)
        assert (c["compiler"], c["main_file"]) == ("pdflatex", "main.tex")
        assert c["problems"] == []
        assert c["duration_ms"] > 0
        assert c["created_at"].endswith("Z")
        assert c["pdf_url"] == f"/api/projects/{pid}/compiles/{c['id']}/pdf"
        assert c["log_url"] == f"/api/projects/{pid}/compiles/{c['id']}/log"

        pdf = client.get(c["pdf_url"])
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF-")
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.headers["content-disposition"].startswith("inline")
        assert pdf.headers["x-content-type-options"] == "nosniff"
        assert pdf.headers["content-security-policy"] == "sandbox"
        log = client.get(c["log_url"])
        assert log.headers["content-type"] == "text/plain; charset=utf-8"
        assert "Output written on main.pdf" in log.text

        assert client.get(f"/api/projects/{pid}/compiles/{c['id']}").json() == c
        assert client.get(f"/api/projects/{pid}/compiles/latest").json()["id"] == c["id"]
        assert client.get(f"/api/projects/{pid}/compiles/latest?ref=live").json()["id"] == c["id"]

        # The same files again give the same compile, without TeX.
        assert _compile(client, pid)["id"] == c["id"]
        assert client.post(f"/api/projects/{pid}/compile", json={}).json()["id"] == c["id"]
        assert service.job_count() == before + 1

        # A change is a new compile.
        client.put(f"/api/projects/{pid}/files/sections/intro.tex", content=INTRO + b"More.\n")
        again = _compile(client, pid)
        assert again["id"] != c["id"]
        assert service.job_count() == before + 2
        assert client.get(f"/api/projects/{pid}/compiles/latest").json()["id"] == again["id"]

        assert client.get(f"/api/projects/{pid}/compiles/{uuid.uuid4()}").status_code == 404
        assert client.get(f"/api/projects/{pid}/compiles/nope").status_code == 404


def test_tags_reuse_the_compile_of_the_same_files(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        never = client.get(f"/api/projects/{pid}/compiles/latest")
        assert (never.status_code, never.json()) == (200, None)
        live = _compile(client, pid)
        before = service.job_count()

        version = client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"}).json()
        tagged = _compile(client, pid, "submission")
        # The same files as live. No TeX, but a compile of its own for the tag.
        assert service.job_count() == before
        assert tagged["id"] != live["id"]
        assert (tagged["ref"], tagged["commit"]) == ("submission", version["commit"])
        assert client.get(tagged["pdf_url"]).content == client.get(live["pdf_url"]).content
        # latest finds compiles of a tag's commit, also by the commit.
        latest = client.get(f"/api/projects/{pid}/compiles/latest?ref={version['commit']}").json()
        assert latest["commit"] == version["commit"]

        assert client.get(f"/api/projects/{pid}/compiles/latest?ref=nothing").status_code == 404
        r = client.post(f"/api/projects/{pid}/compile", json={"ref": "nothing"})
        assert r.status_code == 404
        r = client.post(f"/api/projects/{pid}/compile", json={"ref": "-bad"})
        assert r.status_code == 422


def test_concurrent_requests_for_one_hash_compile_once(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        before = service.job_count()
        results: list[dict] = []

        def ask() -> None:
            results.append(_compile(client, pid))

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert len(results) == 4
        assert {r["id"] for r in results} == {results[0]["id"]}
        assert results[0]["status"] == "success"
        assert service.job_count() == before + 1


def test_problems(service, tmp_path):
    with running_app() as client:
        sign_in(client)
        pid = _project(client, {"main.tex": BROKEN, "sections/intro.tex": BROKEN_INTRO})
        c = _compile(client, pid)
        assert c["status"] == "errors"
        assert c["pdf_url"]
        problems = c["problems"]
        assert {"level": "error", "path": "sections/intro.tex", "line": 2, "message": "Undefined control sequence."} in problems
        warning = next(p for p in problems if "nowhere" in p["message"])
        assert (warning["level"], warning["path"], warning["line"]) == ("warning", "main.tex", 3)
        badbox = next(p for p in problems if p["level"] == "badbox")
        assert (badbox["path"], badbox["line"]) == ("main.tex", 7)
        assert badbox["message"].startswith("Overfull \\hbox")
        # Problems of TeX's own files, or without a place, carry no path.
        assert all(p["path"] in (None, "main.tex", "sections/intro.tex") for p in problems)

        # The compile service's own refusals are problems too.
        outside = tmp_path / "outside.bib"
        outside.write_text("@misc{k, title={Outside}}\n")
        client.put(f"/api/projects/{pid}/files/main.tex", content=(
            b"\\documentclass{article}\n\\usepackage[backend=biber]{biblatex}\n"
            b"\\addbibresource{" + str(outside).encode() + b"}\n\\begin{document}\nx\\cite{k}\n\\end{document}\n"
        ))
        refused = _compile(client, pid)
        assert refused["status"] == "errors"
        message = f"biber does not read {outside}, which is outside the project."
        assert {"level": "error", "path": None, "line": None, "message": message} in refused["problems"]

        # No PDF at all.
        client.put(f"/api/projects/{pid}/files/main.tex", content=b"\\documentclass{article}\n\\begin{document}\n")
        failed = _compile(client, pid)
        assert failed["status"] == "failure"
        assert failed["pdf_url"] is None
        assert client.get(f"/api/projects/{pid}/compiles/{failed['id']}/pdf").status_code == 404
        assert "latexmk" in client.get(failed["log_url"]).text


def test_main_file_and_compiler_come_from_the_project():
    with running_app() as client:
        sign_in(client)
        r = client.post("/api/projects", json={"name": "fonts-paper", "compiler": "xelatex", "main_file": "paper/main.tex"})
        assert r.status_code == 201, r.text
        p = r.json()
        assert p["compiler"] == "xelatex"
        assert client.post("/api/projects", json={"name": "bad-paper", "compiler": "tex"}).status_code == 422
        r = client.patch(f"/api/projects/{p['id']}", json={"compiler": "context"})
        assert r.status_code == 422
        assert client.patch(f"/api/projects/{p['id']}", json={"compiler": "lualatex"}).json()["compiler"] == "lualatex"
        assert client.patch(f"/api/projects/{p['id']}", json={"compiler": None}).json()["compiler"] == "pdflatex"
        client.patch(f"/api/projects/{p['id']}", json={"compiler": "xelatex"})

        client.put(f"/api/projects/{p['id']}/files/paper/main.tex", content=HELLO)
        client.put(f"/api/projects/{p['id']}/files/paper/sections/intro.tex", content=INTRO)
        c = _compile(client, p["id"])
        assert (c["status"], c["compiler"], c["main_file"]) == ("success", "xelatex", "paper/main.tex")
        assert client.get(c["log_url"]).text.startswith("This is XeTeX")


def test_without_a_compile_service(monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        sign_in(client)
        pid = _project(client)

        monkeypatch.setattr(get_settings(), "compile_url", "")
        r = client.post(f"/api/projects/{pid}/compile")
        assert r.status_code == 503
        assert "COMPILE_URL" in r.json()["detail"]
        # Versions are made as before, without a PDF.
        v = client.post(f"/api/projects/{pid}/versions", json={"kind": "snapshot"}).json()
        assert v["compiled"] is None

        monkeypatch.setattr(get_settings(), "compile_url", "http://127.0.0.1:1")
        r = client.post(f"/api/projects/{pid}/compile")
        assert r.status_code == 503
        assert "cannot be reached" in r.json()["detail"]
        assert client.post(f"/api/projects/{pid}/pdfdiff", json={"from": "snapshot-1"}).status_code == 503
        # Nothing was left behind.
        assert client.get(f"/api/projects/{pid}/compiles/latest").json() is None


def test_wrong_token_is_a_503(service, monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        monkeypatch.setattr(get_settings(), "compile_token", "wrong")
        r = client.post(f"/api/projects/{pid}/compile")
        assert r.status_code == 503
        assert "COMPILE_TOKEN" in r.json()["detail"]


def _wait_compiled(client, pid: str, vref: str) -> dict:
    url = f"/api/projects/{pid}/versions/{vref}"
    assert wait_for(lambda: client.get(url).json()["compiled"]["status"] != "running", timeout=60)
    return client.get(url).json()


def test_versions_get_a_compiled_pdf(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        r = client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"})
        assert r.status_code == 201
        made = r.json()
        # The request did not wait for TeX.
        assert made["compiled"]["status"] in ("running", "success")
        v = _wait_compiled(client, pid, "submission")
        compiled = v["compiled"]
        assert set(compiled) == {"id", "status", "pdf_url", "log_url"}
        assert compiled["log_url"] == f"/api/projects/{pid}/compiles/{compiled['id']}/log"
        assert compiled["status"] == "success"
        assert client.get(compiled["pdf_url"]).content.startswith(b"%PDF-")
        row = client.get(f"/api/projects/{pid}/compiles/{compiled['id']}").json()
        assert (row["ref"], row["commit"]) == ("submission", v["commit"])
        listed = client.get(f"/api/projects/{pid}/versions").json()
        assert listed[0]["compiled"]["id"] == compiled["id"]
        detail = client.get(f"/api/projects/{pid}").json()
        assert detail["versions"][0]["compiled"] == compiled

        # A version of files that do not compile has its compile too.
        client.put(f"/api/projects/{pid}/files/main.tex", content=b"\\documentclass{article}\n\\begin{document}\n")
        client.post(f"/api/projects/{pid}/versions", json={"kind": "revision"})
        broken = _wait_compiled(client, pid, "revision-1")
        assert broken["compiled"]["status"] == "failure"
        assert broken["compiled"]["pdf_url"] is None


def test_version_compile_records_an_unreachable_service(monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        monkeypatch.setattr(get_settings(), "compile_url", "http://127.0.0.1:1")
        client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"})
        v = _wait_compiled(client, pid, "submission")
        assert (v["compiled"]["status"], v["compiled"]["pdf_url"]) == ("unavailable", None)
        assert "cannot be reached" in client.get(v["compiled"]["log_url"]).text


def test_pdfdiff(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"})
        client.put(f"/api/projects/{pid}/files/main.tex", content=HELLO.replace(b"Hello world.", b"Hello brave world."))
        r = client.post(f"/api/projects/{pid}/pdfdiff", json={"from": "submission", "to": "live"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert set(d) == COMPILE_KEYS
        assert (d["kind"], d["ref"], d["commit"], d["status"]) == ("pdfdiff", "submission..live", None, "success")
        pdf = client.get(d["pdf_url"])
        assert pdf.content.startswith(b"%PDF-")
        assert "-diff-" in pdf.headers["content-disposition"]
        # Cached like any other compile, and never mistaken for one.
        assert client.post(f"/api/projects/{pid}/pdfdiff", json={"from": "submission"}).json()["id"] == d["id"]
        assert client.get(f"/api/projects/{pid}/compiles/latest").json() is None

        assert client.post(f"/api/projects/{pid}/pdfdiff", json={"from": "nothing"}).status_code == 404
        assert client.post(f"/api/projects/{pid}/pdfdiff", json={}).status_code == 422


def test_synctex(service):
    from app.database import SessionLocal
    from app.models import Compile

    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        c = _compile(client, pid)
        base = f"/api/projects/{pid}/compiles/{c['id']}/synctex"

        r = client.post(f"{base}/view", json={"path": "sections/intro.tex", "line": 3})
        assert r.status_code == 200, r.text
        boxes = r.json()["boxes"]
        assert boxes and set(boxes[0]) == {"page", "x", "y", "width", "height"}
        box = boxes[0]
        r = client.post(f"{base}/edit", json={"page": 1, "x": box["x"] + 5, "y": box["y"] + box["height"] / 2})
        assert r.json() == {"path": "sections/intro.tex", "line": 3, "column": 0}

        assert client.post(f"{base}/edit", json={"page": 9, "x": 10, "y": 10}).status_code == 404
        assert client.post(f"{base}/view", json={"path": "nope.tex", "line": 1}).status_code == 422
        assert client.post(f"{base}/edit", json={"page": 0, "x": 1, "y": 1}).status_code == 422

        # The service forgot the job.
        with SessionLocal() as db:
            db.get(Compile, c["id"]).job_id = uuid.uuid4().hex
            db.commit()
        r = client.post(f"{base}/view", json={"path": "main.tex", "line": 3})
        assert r.status_code == 410
        assert client.post(f"{base}/edit", json={"page": 1, "x": 100, "y": 100}).status_code == 410


def test_roles(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        c = _compile(client, pid)
        from app.database import SessionLocal
        from app.models import Membership

        for role, email in (("viewer", "vera@example.com"), ("reviewer", "rita@example.com")):
            uid, _email, _token = make_user(email)
            with SessionLocal() as db:
                db.add(Membership(project_id=pid, user_id=uid, role=role))
                db.commit()
            with second_client(email) as other:
                # Viewers and reviewers compile too, because they read the PDF.
                assert _compile(other, pid)["id"] == c["id"]
                assert other.get(c["pdf_url"]).status_code == 200
                assert other.get(f"/api/projects/{pid}/compiles/latest").status_code == 200
                r = other.post(f"/api/projects/{pid}/compiles/{c['id']}/synctex/view", json={"path": "main.tex", "line": 3})
                assert r.status_code == 200

        with second_client("eve@example.com") as eve:
            assert eve.post(f"/api/projects/{pid}/compile").status_code == 404
            assert eve.get(c["pdf_url"]).status_code == 404
            assert eve.get(c["log_url"]).status_code == 404
            assert eve.get(f"/api/projects/{pid}/compiles/latest").status_code == 404
            assert eve.post(f"/api/projects/{pid}/pdfdiff", json={"from": "HEAD"}).status_code == 404
