"""The compile service on its own (compile/service.py, docs/DESIGN.md §14),
run natively with this machine's TeX Live."""

import hashlib
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import uuid

import httpx
import pytest
from compile_helpers import (
    HELLO,
    INTRO,
    SERVICE,
    TOKEN,
    compile_service,
    needs_tex,
    tgz,
)

pytestmark = needs_tex

AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    with compile_service(tmp_path_factory.mktemp("compile"), MEERTEX_PROBE="probe-file") as svc:
        yield svc


@pytest.fixture(scope="module")
def strict(tmp_path_factory):
    """A service with a short timeout, small limits and room for two jobs."""
    with compile_service(
        tmp_path_factory.mktemp("strict"),
        COMPILE_TIMEOUT_SECONDS=4,
        COMPILE_KEEP_JOBS=2,
        COMPILE_MAX_TREE_BYTES=100_000,
        COMPILE_MAX_FILES=50,
    ) as svc:
        yield svc


def _job(svc, files: dict[str, bytes], main: str = "main.tex", compiler: str = "pdflatex", path: str = "/jobs"):
    return httpx.post(
        svc.url + path,
        params={"main_file": main, "compiler": compiler},
        content=tgz(files),
        headers=AUTH,
        timeout=60,
    )


def _ok(svc, files: dict[str, bytes], **kwargs) -> dict:
    r = _job(svc, files, **kwargs)
    assert r.status_code == 200, r.text
    return r.json()


def test_success_with_pdf_and_log(service):
    job = _ok(service, {"main.tex": HELLO, "sections/intro.tex": INTRO})
    assert job["status"] == "success"
    assert set(job) == {"job_id", "status", "duration_ms", "log"}
    assert job["duration_ms"] > 0
    assert "Output written on main.pdf" in job["log"]
    assert "latexmk (the last lines" not in job["log"]

    pdf = httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH)
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-")
    log = httpx.get(f"{service.url}/jobs/{job['job_id']}/output.log", headers=AUTH)
    assert log.text == job["log"]


@pytest.mark.parametrize("compiler", ["xelatex", "lualatex"])
def test_other_compilers(service, compiler):
    job = _ok(service, {"main.tex": HELLO, "sections/intro.tex": INTRO}, compiler=compiler)
    assert job["status"] == "success", job["log"][-2000:]


def test_main_file_in_a_folder(service):
    job = _ok(service, {"paper/main.tex": HELLO, "paper/sections/intro.tex": INTRO}, main="paper/main.tex")
    assert job["status"] == "success"
    assert httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH).status_code == 200


def test_errors_failure_and_bad_parameters(service):
    broken = HELLO.replace(b"Hello world.", b"Hello \\undefinedmacro world.")
    job = _ok(service, {"main.tex": broken, "sections/intro.tex": INTRO})
    assert job["status"] == "errors"
    assert "./main.tex:3: Undefined control sequence." in job["log"]
    # latexmk's own summary comes after the TeX log.
    assert "=== latexmk (the last lines of its output) ===" in job["log"]
    assert httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH).status_code == 200

    job = _ok(service, {"main.tex": HELLO})  # sections/intro.tex is missing
    assert job["status"] == "failure"
    assert "sections/intro" in job["log"]
    r = httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH)
    assert r.status_code == 404

    # A main.pdf that comes with the files does not pass for a result.
    job = _ok(service, {"main.tex": b"\\documentclass{article}\n\\begin{document}\n", "main.pdf": b"%PDF-1.5 old"})
    assert job["status"] == "failure"

    job = _ok(service, {"other.tex": HELLO})
    assert job["status"] == "failure"
    assert "The main file main.tex is not in the project" in job["log"]

    assert _job(service, {"main.tex": HELLO}, compiler="context").status_code == 400
    assert _job(service, {"main.tex": HELLO}, main="../main.tex").status_code == 400
    assert _job(service, {"main.tex": HELLO}, main="/etc/main.tex").status_code == 400
    # latexmk puts the main file's name into a shell command, in double quotes.
    for name in ("b`touch x`.tex", "a$(touch x).tex", "-norc.tex", "sub/-x.tex"):
        r = _job(service, {name: HELLO}, main=name)
        assert r.status_code == 400, name
        assert "cannot be compiled" in r.json()["detail"]
    # Spaces and umlauts are fine.
    folder = {"Mein Paper v2/Grüße.tex": HELLO, "Mein Paper v2/sections/intro.tex": INTRO}
    assert _ok(service, folder, main="Mein Paper v2/Grüße.tex")["status"] == "success"


def test_timeout_kills_the_process_group(strict):
    name = f"loop-{uuid.uuid4().hex[:8]}"
    loop = b"\\documentclass{article}\n\\begin{document}\n\\def\\x{\\x}\\x\n\\end{document}\n"
    job = _ok(strict, {f"{name}.tex": loop}, main=f"{name}.tex")
    assert job["status"] == "timeout"
    assert "longer than 4 seconds" in job["log"]
    assert 3500 < job["duration_ms"] < 15000
    # Neither latexmk nor TeX is left running.
    ps = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True, check=False)
    assert ps.stdout.strip() == ""


def test_synctex_both_ways(service):
    job = _ok(service, {"main.tex": HELLO, "sections/intro.tex": INTRO})
    base = f"{service.url}/jobs/{job['job_id']}/synctex"

    r = httpx.post(f"{base}/view", json={"file": "sections/intro.tex", "line": 3, "column": 0}, headers=AUTH)
    assert r.status_code == 200, r.text
    boxes = r.json()
    assert boxes
    box = boxes[0]
    assert set(box) == {"page", "x", "y", "width", "height"}
    assert box["page"] == 1
    # PDF points from the top left of an A4 or letter page.
    assert 0 < box["x"] < 612 and 0 < box["y"] < 842 and box["width"] > 0 and box["height"] > 0

    point = {"page": 1, "x": box["x"] + 5, "y": box["y"] + box["height"] / 2}
    r = httpx.post(f"{base}/edit", json=point, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"file": "sections/intro.tex", "line": 3, "column": 0}

    main_line = httpx.post(f"{base}/view", json={"file": "main.tex", "line": 3}, headers=AUTH).json()
    assert main_line and main_line[0]["y"] < box["y"]

    # SyncTeX answers with the nearest line anywhere on a page, and with
    # nothing for a page the PDF does not have.
    r = httpx.post(f"{base}/edit", json={"page": 9, "x": 100, "y": 100}, headers=AUTH)
    assert r.status_code == 422
    r = httpx.post(f"{base}/view", json={"file": "../etc/passwd", "line": 1}, headers=AUTH)
    assert r.status_code == 422
    r = httpx.post(f"{base}/edit", json={"page": "one"}, headers=AUTH)
    assert r.status_code == 400
    r = httpx.post(f"{service.url}/jobs/{'0' * 32}/synctex/edit", json=point, headers=AUTH)
    assert r.status_code == 404


def test_latexdiff(service):
    old = {"main.tex": HELLO, "sections/intro.tex": INTRO}
    new = {"main.tex": HELLO.replace(b"Hello world.", b"Hello brave new world."), "sections/intro.tex": INTRO + b"More.\n"}
    files = {f"old/{p}": d for p, d in old.items()} | {f"new/{p}": d for p, d in new.items()}
    job = _ok(service, files, path="/latexdiff")
    assert job["status"] == "success", job["log"][-3000:]
    diff = (service.jobs / job["job_id"] / "tree" / "main.tex").read_text()
    assert "\\DIFadd" in diff
    # --flatten pasted the section in.
    assert "Intro line one." in diff
    assert httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH).status_code == 200

    # latexdiff is Perl and reads what \input names, so a path outside the
    # project is refused before it runs.
    sneaky = files | {"new/main.tex": HELLO.replace(b"sections/intro", b"/etc/hostname")}
    job = _ok(service, sneaky, path="/latexdiff")
    assert job["status"] == "failure"
    assert "outside the project" in job["log"]

    missing = {"old/other.tex": HELLO} | {f"new/{p}": d for p, d in new.items()}
    assert _ok(service, missing, path="/latexdiff")["status"] == "failure"
    assert _job(service, {"elsewhere/main.tex": HELLO}, path="/latexdiff").status_code == 400


def test_documents_stay_in_their_folder(service, tmp_path):
    outside = tmp_path / "escaped.txt"
    write = (
        b"\\documentclass{article}\n\\newwrite\\w\n"
        b"\\immediate\\openout\\w=" + str(outside).encode() + b"\n\\immediate\\write\\w{x}\n"
        b"\\begin{document}x\\end{document}\n"
    )
    assert _ok(service, {"main.tex": write})["status"] != "success"
    assert not outside.exists()

    up = b"\\documentclass{article}\n\\newwrite\\w\n\\immediate\\openout\\w=../up.txt\n\\begin{document}x\\end{document}\n"
    job = _ok(service, {"main.tex": up})
    assert job["status"] != "success"
    assert not (service.jobs / job["job_id"] / "up.txt").exists()

    read = b"\\documentclass{article}\n\\begin{document}\n\\input{/etc/hostname}\n\\end{document}\n"
    assert _ok(service, {"main.tex": read})["status"] == "failure"

    # A latexmkrc is Perl. latexmk runs with -norc and never reads it.
    marker = tmp_path / "rc-ran"
    rc = f'system("touch", "{marker}");\n'.encode()
    job = _ok(service, {"main.tex": HELLO, "sections/intro.tex": INTRO, "latexmkrc": rc, ".latexmkrc": rc})
    assert job["status"] == "success"
    assert not marker.exists()

    # No shell escape.
    shell = b"\\documentclass{article}\n\\begin{document}\n\\immediate\\write18{touch " + str(marker).encode()
    shell += b"}\nx\\end{document}\n"
    _ok(service, {"main.tex": shell})
    assert not marker.exists()


def _biblatex(resource: bytes, option: bytes = b"") -> bytes:
    return (
        b"\\documentclass{article}\n\\usepackage[backend=biber]{biblatex}\n"
        b"\\addbibresource" + option + b"{" + resource + b"}\n"
        b"\\begin{document}\nSee \\cite{k}.\n\\printbibliography\n\\end{document}\n"
    )


def test_biber_reads_only_the_projects_bibliographies(service, tmp_path):
    bib = b"@misc{k, title={Probe of the bibliography}}\n"
    job = _ok(service, {"main.tex": _biblatex(b"refs.bib"), "refs.bib": bib})
    assert job["status"] == "success", job["log"][-3000:]
    assert "Probe of the bibliography" in (service.jobs / job["job_id"] / "tree" / "main.bbl").read_text()

    # biber is Perl and not bound by kpathsea. It would read any path.
    outside = tmp_path / "outside.bib"
    outside.write_bytes(bib)
    job = _ok(service, {"main.tex": _biblatex(str(outside).encode()), "refs.bib": bib})
    assert job["status"] == "errors"
    assert f"meertex-compile: biber does not read {outside}, which is outside the project." in job["log"]
    bbl = service.jobs / job["job_id"] / "tree" / "main.bbl"
    assert not bbl.exists() or "Probe of the bibliography" not in bbl.read_text()


@pytest.mark.parametrize("source", ["/etc/x.bib", "../x.bib", "a/../../x.bib", "http://127.0.0.1:9/x.bib",
                                    "file:///etc/x.bib", "~/x.bib"])
def test_the_biber_guard_refuses(tmp_path, source):
    """The guard reads the data sources from the .bcf, where TeX has expanded
    every macro, and stops before biber runs."""
    bcf = f'<bcf:datasource type="file" datatype="bibtex" glob="false">{source}</bcf:datasource>'
    (tmp_path / "main.bcf").write_text(f"<bcf:controlfile>{bcf}</bcf:controlfile>")
    cmd = [sys.executable, "-I", str(SERVICE), "--biber-guard", "main.bcf"]
    out = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 2, out.stderr
    assert "outside the project" in out.stderr


def _member(name: str, kind: bytes, **attrs) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    for key, value in attrs.items():
        setattr(info, key, value)
    return info


def test_unpacking_refuses_what_is_not_a_plain_file(strict, tmp_path):
    def post(body: bytes) -> httpx.Response:
        return httpx.post(f"{strict.url}/jobs", content=body, headers=AUTH, timeout=30)

    before = strict.job_count()
    cases = {
        "absolute": tgz({"/tmp/evil.tex": b"x"}),
        "dotdot": tgz({"../evil.tex": b"x"}),
        "deep dotdot": tgz({"a/../../evil.tex": b"x"}),
        "symlink": tgz({"main.tex": HELLO}, [_member("link.tex", tarfile.SYMTYPE, linkname="/etc/passwd")]),
        "hardlink": tgz({"main.tex": HELLO}, [_member("hard.tex", tarfile.LNKTYPE, linkname="main.tex")]),
        "device": tgz({"main.tex": HELLO}, [_member("null", tarfile.CHRTYPE, devmajor=1, devminor=3)]),
        "fifo": tgz({"main.tex": HELLO}, [_member("pipe", tarfile.FIFOTYPE)]),
        "too large": tgz({"main.tex": HELLO, "big.bin": b"\0" * 200_000}),
        "too many": tgz({f"f{i}.tex": b"x" for i in range(60)}),
        "twice": tgz({"main.tex": HELLO, "./main.tex": HELLO}),
        "file and folder": tgz({"a": b"x", "a/b.tex": b"y"}),
        "not a tar.gz": b"PK\x03\x04 a zip, say",
    }
    for case, body in cases.items():
        r = post(body)
        assert r.status_code == 400, (case, r.text)
        assert r.json()["detail"], case
    assert "absolute" in post(cases["absolute"]).json()["detail"]
    assert "link or a device file" in post(cases["symlink"]).json()["detail"]
    assert "larger than 100000 bytes" in post(cases["too large"]).json()["detail"]
    # Nothing was written next to the jobs, and no job was kept.
    assert sorted(p.name for p in strict.jobs.parent.iterdir()) == ["jobs", "service.log"]
    assert strict.job_count() == before


def test_only_the_newest_jobs_are_kept(strict):
    ids = [_ok(strict, {"main.tex": HELLO, "sections/intro.tex": INTRO})["job_id"] for _ in range(3)]
    assert strict.job_count() == 2
    r = httpx.get(f"{strict.url}/jobs/{ids[0]}/output.pdf", headers=AUTH)
    assert r.status_code == 404
    assert httpx.get(f"{strict.url}/jobs/{ids[2]}/output.pdf", headers=AUTH).status_code == 200


def test_token(service):
    assert httpx.get(f"{service.url}/healthz").json() == {"ok": True}
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        r = httpx.post(f"{service.url}/jobs", content=tgz({"main.tex": HELLO}), headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        r = httpx.get(f"{service.url}/jobs/{'0' * 32}/output.log", headers=headers)
        assert r.status_code == 401
    assert httpx.get(f"{service.url}/jobs/{'0' * 32}/output.log", headers=AUTH).status_code == 404


def test_refuses_to_start_without_a_token(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "COMPILE_TOKEN"} | {"COMPILE_JOBS_DIR": str(tmp_path)}
    cmd = [sys.executable, "-I", str(SERVICE)]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode != 0
    assert "COMPILE_TOKEN is not set" in out.stderr


def test_no_settings_reach_the_jobs(service):
    """kpathsea expands $VARIABLES in file names, which shows what is in a
    job's environment: MEERTEX_PROBE is, COMPILE_TOKEN is not."""
    tex = b"\\documentclass{article}\n\\begin{document}\n\\input{$MEERTEX_PROBE}\n\\input{$COMPILE_TOKEN}\nx\\end{document}\n"
    files = {"main.tex": tex, "probe-file.tex": b"Probe.\n", f"{TOKEN}.tex": b"Leaked.\n"}
    job = _ok(service, files)
    assert "(./probe-file.tex" in job["log"]
    assert f"(./{TOKEN}.tex" not in job["log"]


# --- Workspaces, for the compiles of an open editor ------------------------------

WITH_LABEL = rb"""\documentclass{article}
\begin{document}
\section{One}\label{one}
See section \ref{one}.

\input{sections/intro}
\end{document}
"""
LOOP = b"\\documentclass{article}\n\\begin{document}\n\\def\\x{\\x}\\x\n\\end{document}\n"


def _prepare(svc, key: str, files: dict[str, bytes], clean: bool = False) -> dict:
    manifest = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    r = httpx.post(f"{svc.url}/workspaces/{key}/prepare", json={"files": manifest, "clean": clean}, headers=AUTH, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def _in_workspace(svc, key: str, files: dict[str, bytes], main: str = "main.tex", compiler: str = "pdflatex", clean: bool = False):
    """``(the files it asked for, the job)``."""
    plan = _prepare(svc, key, files, clean)
    params = {"main_file": main, "compiler": compiler, "workspace": key, "token": plan["token"]}
    r = httpx.post(svc.url + "/jobs", params=params, content=tgz({p: files[p] for p in plan["need"]}), headers=AUTH, timeout=60)
    assert r.status_code == 200, r.text
    return plan["need"], r.json()


def _tex_runs(svc, job: dict) -> int:
    return len(re.findall(r"Run number \d+ of rule '\w*latex'", (svc.jobs / job["job_id"] / "latexmk.out").read_text()))


def _paper() -> dict[str, bytes]:
    return {"main.tex": WITH_LABEL, "sections/intro.tex": INTRO}


def _tree(svc, key: str):
    return svc.jobs / "workspaces" / key / "tree"


def test_a_workspace_gets_only_what_changed_and_reuses_the_build(service):
    key = str(uuid.uuid4())
    files = {**_paper(), "notes.txt": b"Some notes.\n"}
    need, job = _in_workspace(service, key, files)
    assert sorted(need) == sorted(files)
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) >= 2  # the \ref needs a second pass

    files["sections/intro.tex"] = INTRO + b"More.\n"
    del files["notes.txt"]
    need, job = _in_workspace(service, key, files)
    assert need == ["sections/intro.tex"]
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) == 1
    assert not (_tree(service, key) / "notes.txt").exists()
    pdf = httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH)
    assert pdf.content.startswith(b"%PDF-")
    # A job holds its results and no copy of the paper.
    assert not (service.jobs / job["job_id"] / "tree").exists()

    # Nothing changed: nothing is sent.
    assert _prepare(service, key, files)["need"] == []


def test_a_workspace_notices_what_a_document_left_or_changed(service):
    key = str(uuid.uuid4())
    files = _paper()
    _in_workspace(service, key, files)
    tree = _tree(service, key)
    (tree / "stray.tex").write_text("x")
    (tree / "folder").mkdir()
    (tree / "folder" / "stray.tex").write_text("x")
    os.symlink("/etc", tree / "etc-link")
    (tree / "sections" / "intro.tex").write_text("Rewritten by a run.")
    (tree / "main.tex").unlink()
    os.symlink("/etc/hostname", tree / "main.tex")

    plan = _prepare(service, key, files)
    assert sorted(plan["need"]) == ["main.tex", "sections/intro.tex"]
    assert sorted(p.name for p in tree.iterdir() if p.name != "sections") == ["main.tex"]

    need, job = _in_workspace(service, key, files)
    assert sorted(need) == ["main.tex", "sections/intro.tex"]
    assert job["status"] == "success", job["log"][-2000:]
    assert (tree / "sections" / "intro.tex").read_bytes() == INTRO
    assert not (tree / "main.tex").is_symlink()


def test_a_build_file_that_breaks_the_next_run_is_dropped(service):
    key = str(uuid.uuid4())
    writes = (
        b"\\documentclass{article}\n\\newcommand\\fromaux{}\n\\makeatletter\n"
        b"\\AtEndDocument{\\immediate\\write\\@auxout{\\string\\fromaux}}\n\\makeatother\n"
        b"\\begin{document}\nText.\n\\end{document}\n"
    )
    _need, job = _in_workspace(service, key, {"main.tex": writes})
    assert job["status"] == "success", job["log"][-2000:]

    # The old .aux calls a command that is gone. Only a run without it is right.
    gone = b"\\documentclass{article}\n\\begin{document}\nText changed.\n\\end{document}\n"
    _need, job = _in_workspace(service, key, {"main.tex": gone})
    assert job["status"] == "success", job["log"][-2000:]
    assert ".aux:" not in job["log"]


def test_an_error_in_the_text_keeps_the_build(service):
    key = str(uuid.uuid4())
    files = _paper()
    _in_workspace(service, key, files)
    files["main.tex"] = WITH_LABEL.replace(b"See section", b"See \\undefinedmacro section")
    _need, job = _in_workspace(service, key, files)
    assert job["status"] == "errors"
    assert _tex_runs(service, job) == 1
    files["main.tex"] = WITH_LABEL.replace(b"See section", b"Read section")
    _need, job = _in_workspace(service, key, files)
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) == 1


def test_cancel_stops_the_run_and_keeps_the_last_good_build(service):
    key = str(uuid.uuid4())
    name = f"cancel-{uuid.uuid4().hex[:8]}"
    main = f"{name}.tex"
    files = {main: WITH_LABEL, "sections/intro.tex": INTRO}
    assert httpx.post(f"{service.url}/workspaces/{key}/cancel", headers=AUTH).json() == {"cancelled": False}
    _need, job = _in_workspace(service, key, files, main=main)
    assert job["status"] == "success", job["log"][-2000:]

    answer: dict = {}
    looping = {**files, main: LOOP}
    thread = threading.Thread(target=lambda: answer.update(_in_workspace(service, key, looping, main=main)[1]))
    thread.start()

    def tex_runs() -> bool:
        return subprocess.run(["pgrep", "-f", f"pdflatex.*{name}"], capture_output=True, check=False).returncode == 0

    deadline = time.monotonic() + 20
    while not tex_runs() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert tex_runs()
    r = httpx.post(f"{service.url}/workspaces/{key}/cancel", headers=AUTH)
    assert r.json() == {"cancelled": True}
    thread.join(30)
    assert not thread.is_alive()
    assert (answer["status"], answer["job_id"]) == ("cancelled", None)
    assert not tex_runs()
    assert httpx.post(f"{service.url}/workspaces/{key}/cancel", headers=AUTH).json() == {"cancelled": False}

    # What the stopped run left is gone, and the good run's build is back.
    files[main] = WITH_LABEL.replace(b"See section", b"Read section")
    _need, job = _in_workspace(service, key, files, main=main)
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) == 1


def test_synctex_of_a_workspace_job(service):
    key = str(uuid.uuid4())
    files = {"main.tex": HELLO, "sections/intro.tex": INTRO}
    _need, first = _in_workspace(service, key, files)
    files["main.tex"] = HELLO.replace(b"Hello world.", b"Hello there, world.")
    _in_workspace(service, key, files)

    # The older job still answers, for the PDF it made.
    base = f"{service.url}/jobs/{first['job_id']}/synctex"
    boxes = httpx.post(f"{base}/view", json={"file": "sections/intro.tex", "line": 3}, headers=AUTH).json()
    assert boxes and boxes[0]["page"] == 1
    point = {"page": 1, "x": boxes[0]["x"] + 5, "y": boxes[0]["y"] + boxes[0]["height"] / 2}
    r = httpx.post(f"{base}/edit", json=point, headers=AUTH)
    assert r.json() == {"file": "sections/intro.tex", "line": 3, "column": 0}
    assert httpx.post(f"{base}/view", json={"file": "nope.tex", "line": 1}, headers=AUTH).status_code == 422
    assert httpx.get(f"{service.url}/jobs/{first['job_id']}/output.pdf", headers=AUTH).status_code == 200


def test_a_main_file_in_a_folder_in_a_workspace(service):
    key = str(uuid.uuid4())
    files = {"paper/main.tex": WITH_LABEL, "paper/sections/intro.tex": INTRO}
    _need, job = _in_workspace(service, key, files, main="paper/main.tex")
    assert job["status"] == "success", job["log"][-2000:]
    files["paper/main.tex"] = WITH_LABEL.replace(b"See section", b"Read section")
    _need, job = _in_workspace(service, key, files, main="paper/main.tex")
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) == 1
    base = f"{service.url}/jobs/{job['job_id']}/synctex"
    boxes = httpx.post(f"{base}/view", json={"file": "paper/sections/intro.tex", "line": 3}, headers=AUTH).json()
    assert boxes


def test_clean_and_another_compiler_start_from_nothing(service):
    key = str(uuid.uuid4())
    files = _paper()
    _in_workspace(service, key, files)
    need, job = _in_workspace(service, key, files, clean=True)
    assert need == []
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) >= 2

    _need, job = _in_workspace(service, key, files, compiler="xelatex")
    assert job["status"] == "success", job["log"][-2000:]
    assert _tex_runs(service, job) >= 2
    assert job["log"].startswith("This is XeTeX")


def test_forgetting_a_workspace_removes_its_files_and_jobs(service):
    key = str(uuid.uuid4())
    _need, job = _in_workspace(service, key, _paper())
    assert _tree(service, key).is_dir()
    assert (service.jobs / job["job_id"]).is_dir()
    r = httpx.post(f"{service.url}/workspaces/{key}/forget", headers=AUTH, timeout=30)
    assert r.json() == {"forgotten": True}
    assert not (service.jobs / "workspaces" / key).exists()
    assert not (service.jobs / job["job_id"]).exists()
    # One that never existed is fine as well.
    assert httpx.post(f"{service.url}/workspaces/{uuid.uuid4()}/forget", headers=AUTH, timeout=30).status_code == 200


def test_workspace_requests_that_are_refused(service):
    key = str(uuid.uuid4())
    good = "0" * 64

    def prepare(files) -> httpx.Response:
        return httpx.post(f"{service.url}/workspaces/{key}/prepare", json={"files": files}, headers=AUTH)

    assert prepare({"../main.tex": good}).status_code == 400
    assert prepare({"/etc/passwd": good}).status_code == 400
    assert prepare({"main.tex": "short"}).status_code == 400
    assert prepare({"a": good, "a/b.tex": good}).status_code == 400
    assert prepare("main.tex").status_code == 400

    params = {"main_file": "main.tex", "compiler": "pdflatex"}
    body = tgz({"main.tex": HELLO})
    # A job needs its preparation.
    r = httpx.post(f"{service.url}/jobs", params={**params, "workspace": key, "token": "nope"}, content=body, headers=AUTH)
    assert r.status_code == 409
    r = httpx.post(f"{service.url}/latexdiff", params={**params, "workspace": key}, content=body, headers=AUTH)
    assert r.status_code == 400
    r = httpx.post(f"{service.url}/jobs", params={**params, "workspace": "../up"}, content=body, headers=AUTH)
    assert r.status_code == 400
    assert httpx.post(f"{service.url}/workspaces/NOT-A-KEY/cancel", headers=AUTH).status_code == 404

    for path in ("prepare", "cancel"):
        assert httpx.post(f"{service.url}/workspaces/{key}/{path}", json={"files": {}}).status_code == 401


def test_workspaces_and_their_jobs_are_limited(tmp_path):
    with compile_service(tmp_path / "limits", COMPILE_KEEP_JOBS=2, COMPILE_KEEP_WORKSPACES=2, COMPILE_MAX_TREE_BYTES=100_000) as svc:
        keys = [str(uuid.uuid4()) for _ in range(3)]
        files = {"main.tex": HELLO, "sections/intro.tex": INTRO}
        ids = []
        for number in range(3):
            changed = {**files, "main.tex": HELLO + b"% " + str(number).encode() + b"\n"}
            ids.append(_in_workspace(svc, keys[0], changed)[1]["job_id"])
        # Every paper keeps its own newest jobs, the older ones go.
        assert httpx.get(f"{svc.url}/jobs/{ids[0]}/output.pdf", headers=AUTH).status_code == 404
        assert httpx.get(f"{svc.url}/jobs/{ids[2]}/output.pdf", headers=AUTH).status_code == 200

        _in_workspace(svc, keys[1], files)
        time.sleep(0.05)
        _in_workspace(svc, keys[2], files)
        names = sorted(p.name for p in (svc.jobs / "workspaces").iterdir())
        assert names == sorted(keys[1:]), "the workspace used longest ago goes"
        # Its jobs still have their PDFs.
        assert httpx.get(f"{svc.url}/jobs/{ids[2]}/output.pdf", headers=AUTH).status_code == 200
        boxes = httpx.post(f"{svc.url}/jobs/{ids[2]}/synctex/view", json={"file": "sections/intro.tex", "line": 3}, headers=AUTH)
        assert boxes.status_code == 200 and boxes.json()

        # The cap on the paper's size counts the files that are already there.
        key = str(uuid.uuid4())
        big = {"main.tex": HELLO, "sections/intro.tex": INTRO, "a.bin": b"\1" * 60_000}
        assert _in_workspace(svc, key, big)[1]["status"] == "success"
        more = {**big, "b.bin": b"\2" * 60_000}
        plan = _prepare(svc, key, more)
        params = {"main_file": "main.tex", "compiler": "pdflatex", "workspace": key, "token": plan["token"]}
        r = httpx.post(svc.url + "/jobs", params=params, content=tgz({"b.bin": more["b.bin"]}), headers=AUTH, timeout=60)
        assert r.status_code == 400
        assert "larger than 100000 bytes" in r.json()["detail"]
        # After that the workspace asks for everything again.
        assert sorted(_prepare(svc, key, big)["need"]) == sorted(big)
