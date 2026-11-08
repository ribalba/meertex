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


def _editor(client, pid: str, clean: bool = False) -> dict:
    """A compile the way an open editor asks for it."""
    r = client.post(f"/api/projects/{pid}/compile", json={"editor": True, "clean": clean})
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

        def forget() -> None:
            """The service no longer has the jobs, as once it made COMPILE_KEEP_JOBS newer ones."""
            with SessionLocal() as db:
                for row in db.query(Compile).filter(Compile.project_id == pid):
                    row.job_id = uuid.uuid4().hex
                db.commit()

        # A review reads the PDF of a commit. Its files are compiled again for
        # SyncTeX, once for requests that come together (a selection's start
        # and end), and every compile of the same files gets the new job.
        client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"})
        v = _wait_compiled(client, pid, "submission")
        tagged = _compile(client, pid, v["commit"])
        tagged_base = f"/api/projects/{pid}/compiles/{tagged['id']}/synctex"
        forget()
        before = service.job_count()
        point = {"page": 1, "x": box["x"] + 5, "y": box["y"] + box["height"] / 2}
        answers: list = []
        threads = [threading.Thread(target=lambda: answers.append(client.post(f"{tagged_base}/edit", json=point))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert [r.status_code for r in answers] == [200, 200], [r.text for r in answers]
        assert [r.json() for r in answers] == [{"path": "sections/intro.tex", "line": 3, "column": 0}] * 2
        assert service.job_count() == before + 1
        assert client.post(f"{base}/view", json={"path": "sections/intro.tex", "line": 3}).json()["boxes"] == boxes
        assert service.job_count() == before + 1

        # A live compile the text has moved on from cannot be made again.
        client.put(f"/api/projects/{pid}/files/main.tex", content=HELLO.replace(b"Hello world.", b"Hello brave world."))
        forget()
        r = client.post(f"{base}/view", json={"path": "main.tex", "line": 3})
        assert r.status_code == 410
        assert "Compile again" in r.json()["detail"]
        assert client.post(f"{base}/edit", json=point).status_code == 410
        assert client.post(f"{tagged_base}/edit", json=point).json()["line"] == 3


def test_an_editor_compile_sends_only_what_changed(service, monkeypatch):
    from app import compiler

    sent: list[list[str]] = []
    pack = compiler.pack

    def spying(trees):
        sent.append(sorted(path for tree in trees.values() for path in tree))
        return pack(trees)

    monkeypatch.setattr(compiler, "pack", spying)
    with running_app() as client:
        sign_in(client)
        pid = _project(client, {"main.tex": HELLO, "sections/intro.tex": INTRO, "figure.bin": b"\0\1\2" * 1000})
        first = _editor(client, pid)
        assert first["status"] == "success"
        assert (first["kind"], first["ref"], first["commit"]) == ("pdf", "live", None)
        assert sent == [["figure.bin", "main.tex", "sections/intro.tex"]]

        # The same text comes from the cache, without TeX.
        assert _editor(client, pid)["id"] == first["id"]
        assert len(sent) == 1

        client.put(f"/api/projects/{pid}/files/sections/intro.tex", content=INTRO + b"More.\n")
        second = _editor(client, pid)
        assert second["id"] != first["id"]
        assert sent[-1] == ["sections/intro.tex"]
        assert client.get(second["pdf_url"]).content.startswith(b"%PDF-")

        # SyncTeX finds its way in a PDF built in the workspace.
        base = f"/api/projects/{pid}/compiles/{second['id']}/synctex"
        boxes = client.post(f"{base}/view", json={"path": "sections/intro.tex", "line": 3}).json()["boxes"]
        assert boxes
        point = {"page": 1, "x": boxes[0]["x"] + 5, "y": boxes[0]["y"] + boxes[0]["height"] / 2}
        assert client.post(f"{base}/edit", json=point).json()["path"] == "sections/intro.tex"

        # When the service has forgotten the job, the same text is compiled again for it.
        from app.database import SessionLocal
        from app.models import Compile

        with SessionLocal() as db:
            db.get(Compile, second["id"]).job_id = uuid.uuid4().hex
            db.commit()
        before = service.job_count()
        assert client.post(f"{base}/edit", json=point).json()["path"] == "sections/intro.tex"
        assert service.job_count() == before + 1


def test_deleting_a_paper_removes_its_workspace(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        name = client.get(f"/api/projects/{pid}").json()["name"]
        _editor(client, pid)
        assert (service.jobs / "workspaces" / pid).is_dir()
        assert client.delete(f"/api/projects/{pid}", params={"confirm": name}).status_code == 200
        assert not (service.jobs / "workspaces" / pid).exists()


def test_a_version_never_gets_an_editor_build(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        live = _editor(client, pid)
        before = service.job_count()
        client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"})
        version = _wait_compiled(client, pid, "submission")
        assert version["compiled"]["status"] == "success"
        assert version["compiled"]["id"] != live["id"]
        assert service.job_count() == before + 1
        # The CLI asks for a clean build of the live text as well.
        assert _compile(client, pid)["id"] != live["id"]


def test_recompile_from_scratch(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        first = _editor(client, pid)
        before = service.job_count()
        clean = _editor(client, pid, clean=True)
        assert clean["id"] != first["id"]
        assert clean["status"] == "success"
        assert service.job_count() == before + 1
        assert _editor(client, pid)["id"] == clean["id"]


def test_a_newer_edit_stops_the_running_compile(service):
    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        cancel = f"/api/projects/{pid}/compile/cancel"
        assert client.post(cancel).json() == {"cancelled": False}

        loop = b"\\documentclass{article}\n\\begin{document}\n\\def\\x{\\x}\\x\n\\end{document}\n"
        client.put(f"/api/projects/{pid}/files/main.tex", content=loop)
        answers: list = []

        def ask() -> None:
            answers.append(client.post(f"/api/projects/{pid}/compile", json={"editor": True}))

        thread = threading.Thread(target=ask)
        thread.start()
        assert wait_for(lambda: client.post(cancel).json()["cancelled"], timeout=30)
        thread.join(60)
        assert not thread.is_alive()
        assert answers[0].status_code == 409, answers[0].text
        assert "newer edit" in answers[0].json()["detail"]
        # A stopped compile leaves nothing behind.
        assert client.get(f"/api/projects/{pid}/compiles/latest").json() is None

        client.put(f"/api/projects/{pid}/files/main.tex", content=HELLO)
        assert _editor(client, pid)["status"] == "success"


def test_only_the_newest_live_pdfs_are_kept(service, monkeypatch):
    from app.config import get_settings
    from app.database import SessionLocal
    from app.live import blobs
    from app.models import Compile

    monkeypatch.setattr(get_settings(), "compile_keep_live_pdfs", 2)

    def sha_of(compile_id: str) -> str:
        with SessionLocal() as db:
            return db.get(Compile, compile_id).pdf_sha

    with running_app() as client:
        sign_in(client)
        pid = _project(client)
        rows, shas = [], []
        for number in range(3):
            client.put(f"/api/projects/{pid}/files/sections/intro.tex", content=INTRO + f"Edit {number}.\n".encode())
            rows.append(_editor(client, pid))
            shas.append(sha_of(rows[-1]["id"]))

        old = client.get(f"/api/projects/{pid}/compiles/{rows[0]['id']}").json()
        assert (old["status"], old["pdf_url"]) == ("success", None)
        assert client.get(rows[0]["pdf_url"]).status_code == 404
        assert client.get(rows[0]["log_url"]).text == ""
        assert blobs.path(pid, shas[0]) is None
        for row, sha in zip(rows[1:], shas[1:], strict=True):
            assert client.get(row["pdf_url"]).status_code == 200
            assert blobs.path(pid, sha) is not None

        # The text of the compile that lost its PDF is simply compiled again.
        client.put(f"/api/projects/{pid}/files/sections/intro.tex", content=INTRO + b"Edit 0.\n")
        back = _editor(client, pid)
        assert back["id"] != rows[0]["id"]
        assert client.get(back["pdf_url"]).status_code == 200


def test_a_kept_pdf_that_is_also_used_elsewhere_stays(service, monkeypatch):
    from app.config import get_settings
    from app.database import SessionLocal
    from app.live import blobs
    from app.models import Compile

    monkeypatch.setattr(get_settings(), "compile_keep_live_pdfs", 1)

    def sha_of(compile_id: str) -> str:
        with SessionLocal() as db:
            return db.get(Compile, compile_id).pdf_sha

    def edit(client, pid: str, text: bytes) -> None:
        client.put(f"/api/projects/{pid}/files/sections/intro.tex", content=INTRO + text)

    with running_app() as client:
        sign_in(client)
        # Someone downloads the PDF and puts it into the paper.
        pid = _project(client)
        first = _compile(client, pid)
        sha = sha_of(first["id"])
        pdf = client.get(first["pdf_url"]).content
        assert client.put(f"/api/projects/{pid}/files/submitted.pdf", content=pdf).status_code == 200
        edit(client, pid, b"Next.\n")
        _compile(client, pid)
        assert client.get(first["pdf_url"]).status_code == 404
        assert blobs.path(pid, sha) is not None
        assert client.get(f"/api/projects/{pid}/files/submitted.pdf").content == pdf

        # A version made from the same files shares the live compile's PDF.
        other = _project(client)
        live = _compile(client, other)
        client.post(f"/api/projects/{other}/versions", json={"kind": "submission"})
        version = _wait_compiled(client, other, "submission")
        assert sha_of(version["compiled"]["id"]) == sha_of(live["id"])
        edit(client, other, b"Next.\n")
        _compile(client, other)
        assert client.get(version["compiled"]["pdf_url"]).status_code == 200


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
