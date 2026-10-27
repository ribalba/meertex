"""``meertex compile`` (DESIGN §14) against a fake HTTP API: the request, what
it prints, the exit code, and where the PDF goes."""

import json

import httpx
import pytest
from meertex import cli, config
from test_cli import SERVER, FakeApi, run

P = "/api/projects/paper"
CID = "0c0c0c0c-0000-4000-8000-000000000001"
PDF = b"%PDF-1.5 fake\n%%EOF\n"


@pytest.fixture
def fake(monkeypatch):
    f = FakeApi()
    monkeypatch.setenv("MEERTEX_SERVER", SERVER)
    monkeypatch.setenv("MEERTEX_TOKEN", "tok")
    monkeypatch.setattr(cli, "api", lambda: f.api())
    return f


def compiled(status: str = "success", problems: list | None = None, pdf: bool = True) -> dict:
    return {
        "id": CID, "kind": "pdf", "status": status, "ref": "live", "commit": None, "compiler": "pdflatex",
        "main_file": "paper/main.tex", "duration_ms": 1234, "created_at": "2026-10-27T10:00:00Z",
        "problems": problems or [],
        "pdf_url": f"{P}/compiles/{CID}/pdf" if pdf else None, "log_url": f"{P}/compiles/{CID}/log",
    }


def test_compile_prints_problems_and_saves_the_pdf(fake, capsys, tmp_path, monkeypatch):
    problems = [
        {"level": "error", "path": "sections/intro.tex", "line": 2, "message": "Undefined control sequence."},
        {"level": "warning", "path": None, "line": None, "message": "There were undefined references."},
    ]
    fake.routes[("POST", f"{P}/compile")] = compiled("errors", problems)
    fake.routes[("GET", f"{P}/compiles/{CID}/pdf")] = httpx.Response(200, content=PDF)
    monkeypatch.chdir(tmp_path)

    code, out, _err = run(capsys, "compile", "paper")
    assert code == 1
    assert fake.calls[0] == ("POST", f"{P}/compile", {}, {"ref": "live"})
    assert "errors  live  pdflatex  paper/main.tex  1.2 s" in out
    assert "error    sections/intro.tex:2  Undefined control sequence." in out
    assert "warning  There were undefined references." in out
    assert (tmp_path / "paper.pdf").read_bytes() == PDF

    fake.routes[("POST", f"{P}/compile")] = compiled()
    code, out, _err = run(capsys, "compile", "paper", "--ref", "submission", "--out", "out/x.pdf", "--json")
    assert code == 0
    assert fake.calls[-2][3] == {"ref": "submission"}
    assert json.loads(out)["saved"] == "out/x.pdf"
    assert (tmp_path / "out" / "x.pdf").read_bytes() == PDF


def test_a_checkout_gets_the_pdf_that_sync_ignores(fake, capsys, tmp_path):
    root = tmp_path / "paper"
    root.mkdir()
    config.write_project(root, {"project_id": "paper", "name": "paper", "main_file": "paper/main.tex"})
    fake.routes[("POST", f"{P}/compile")] = compiled()
    fake.routes[("GET", f"{P}/compiles/{CID}/pdf")] = httpx.Response(200, content=PDF)
    code, out, _err = run(capsys, "compile", str(root))
    assert code == 0
    assert (root / "main.pdf").read_bytes() == PDF
    assert f"Saved {root / 'main.pdf'}" in out


def test_no_pdf_and_no_service(fake, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake.routes[("POST", f"{P}/compile")] = compiled("failure", pdf=False)
    code, _out, err = run(capsys, "compile", "paper")
    assert code == 1
    assert f"/compiles/{CID}/log" in err
    assert list(tmp_path.iterdir()) == []

    fake.routes[("POST", f"{P}/compile")] = (503, {"detail": "Compiling is off on this server. COMPILE_URL is not set."})
    code, _out, err = run(capsys, "compile", "paper")
    assert code == 1
    assert "COMPILE_URL is not set" in err
