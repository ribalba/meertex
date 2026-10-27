"""The reference check over HTTP (docs/DESIGN.md §16): the report, runs, the
checked mark, fields, adding entries and the editor's problems. The lookups
are answered by tests/references_fakes.py."""

import pytest
from helpers import needs_db, running_app, sign_in
from references_fakes import FakeSources, found, page, rec
from test_live_helpers import bare_client, make_project, make_user

from app.references import service

pytestmark = needs_db

BIB = r"""@inproceedings{roussou2019,
  title = {Transformation through Provocation?},
  author = {Roussou, Maria and Perry, Sara},
  booktitle = {Proceedings of CHI '19},
  year = {2019},
  doi = {10.1145/3290605.3300857},
}

@article{fake2023,
  title = {Quantum Carbon Accounting for Large Language Models},
  author = {Smith, John and Doe, Jane},
  journal = {Journal of Green Computing},
  year = {2023},
  doi = {10.1145/9999999.9999999},
}

@misc{gmt,
  title = {Green Metrics Tool},
  url = {https://www.green-coding.io/products/green-metrics-tool/},
  urldate = {2026-01-10},
}

@article{todo,
  title = {TODO},
  author = {Author Name},
  year = {2031},
}

@article{broken,
  title = {Never closed
"""

TEX = r"""\documentclass{article}
\begin{document}
Provocation changes visitors \cite{roussou2019}.
Carbon accounting is solved \citep{fake2023, nothere}.
We measured with \cite{gmt}.
\bibliography{refs}
\end{document}
"""

ROUSSOU = rec(
    "doi.org",
    type="paper-conference",
    doi="10.1145/3290605.3300857",
    title="Transformation through Provocation?",
    people=["Maria Roussou", "Sara Perry", "Akrivi Katifori"],
    years=[2019],
    venue="Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems",
    pages="1-13",
    publisher="ACM",
    abstract="We provoke museum visitors.",
)


def fakes() -> FakeSources:
    return FakeSources(
        doi={"10.1145/3290605.3300857": found(ROUSSOU)},
        crossref_prefix={"10.1145": found(name="Association for Computing Machinery (ACM)")},
        page={"https://www.green-coding.io/products/green-metrics-tool/": page(
            "https://www.green-coding.io/products/green-metrics-tool/", 200, title="Green Metrics Tool | Green Coding")},
    )


@pytest.fixture
def fake(monkeypatch):
    src = fakes()
    monkeypatch.setattr(service, "make_sources", lambda refresh: src)
    return src


def _setup(client):
    me = sign_in(client)
    pid, _name = make_project(me["id"])
    assert client.put(f"/api/projects/{pid}/files/refs.bib", content=BIB.encode()).status_code == 200
    assert client.put(f"/api/projects/{pid}/files/main.tex", content=TEX.encode()).status_code == 200
    return me, pid


def _entries(client, pid) -> dict:
    r = client.get(f"/api/projects/{pid}/references")
    assert r.status_code == 200, r.text
    return {e["key"]: e for e in r.json()["entries"]}


def _codes(entry) -> set[str]:
    return {f["code"] for f in entry["findings"]}


def _run(client, pid, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/references/check", json=body)
    assert r.status_code == 202, r.text
    run = r.json()
    service.wait(pid)
    r = client.get(f"/api/projects/{pid}/references/check/{run['id']}")
    assert r.status_code == 200
    return r.json()


def test_report_before_any_run():
    with running_app() as client:
        _me, pid = _setup(client)
        r = client.get(f"/api/projects/{pid}/references")
        data = r.json()
        assert data["files"] == ["refs.bib"]
        assert data["run"] is None
        assert data["network_enabled"] is True
        assert [b["line"] for b in data["broken"]] == [29]
        assert data["missing"] == [{"key": "nothere", "path": "main.tex", "line": 4}]
        entries = {e["key"]: e for e in data["entries"]}
        assert set(entries) == {"roussou2019", "fake2023", "gmt", "todo"}
        assert entries["roussou2019"]["status"] == "pending"
        assert entries["roussou2019"]["cites"] == [{"path": "main.tex", "line": 3}]
        todo = entries["todo"]
        assert todo["status"] == "error"
        assert {"placeholder", "year", "not_cited"} <= _codes(todo)
        assert entries["gmt"]["status"] == "pending"
        # The broken entry and the error count. Pending ones do not.
        assert data["problems"] == 2
        assert client.get(f"/api/projects/{pid}/references/summary").json() == {"problems": 2, "total": 4}


def test_a_run_looks_entries_up(fake):
    with running_app() as client:
        _me, pid = _setup(client)
        run = _run(client, pid)
        assert run["status"] == "done"
        assert run["done"] == run["total"] == 4
        entries = _entries(client, pid)

        roussou = entries["roussou2019"]
        assert roussou["record"]["title"] == "Transformation through Provocation?"
        assert roussou["checked_at"] is not None
        authors = next(f for f in roussou["findings"] if f["code"] == "authors")
        assert authors["level"] == "warning"
        assert "Akrivi Katifori" in authors["message"]
        assert authors["record_value"] == "Roussou, Maria and Perry, Sara and Katifori, Akrivi"
        assert roussou["status"] == "warning"
        assert {f["field"] for f in roussou["findings"] if f["code"] == "missing"} == {"pages"}

        fake2023 = entries["fake2023"]
        assert fake2023["status"] == "error"
        nf = next(f for f in fake2023["findings"] if f["code"] == "doi_not_found")
        assert "Association for Computing Machinery (ACM)" in nf["message"]

        assert entries["gmt"]["status"] == "verified"
        assert entries["gmt"]["record"]["source"] == "web"

        # A second run finds everything fresh and looks nothing up again.
        before = len(fake.calls)
        again = _run(client, pid)
        assert again["total"] == 0
        assert len(fake.calls) == before


def test_mark_change_and_unmark(fake):
    with running_app() as client:
        _me, pid = _setup(client)
        _run(client, pid)
        r = client.post(f"/api/projects/{pid}/references/roussou2019/checked")
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["status"] == "checked"
        assert entry["mark"]["valid"] is True
        assert entry["mark"]["hash"] == entry["fingerprint"][:8]
        text = client.get(f"/api/projects/{pid}/files/refs.bib").text
        assert "meertex-checked = {" in text
        assert f"{entry['mark']['date']}, didi, {entry['fingerprint'][:8]}" in text

        # Reformatting keeps the mark, a changed value breaks it.
        client.put(f"/api/projects/{pid}/files/refs.bib", content=text.replace("  title = ", "    title   =   ").encode())
        assert _entries(client, pid)["roussou2019"]["status"] == "checked"
        text = client.get(f"/api/projects/{pid}/files/refs.bib").text
        client.put(f"/api/projects/{pid}/files/refs.bib", content=text.replace("{2019}", "{2018}", 1).encode())
        changed = _entries(client, pid)["roussou2019"]
        assert changed["status"] == "changed"
        # roussou2019 is changed, fake2023 and todo are errors, one entry is broken.
        assert client.get(f"/api/projects/{pid}/references/summary").json()["problems"] == 4
        problems = client.get(f"/api/projects/{pid}/references/problems").json()
        assert any("changed after it was marked" in p["message"] for p in problems)

        # Marking again writes one mark, not two.
        client.post(f"/api/projects/{pid}/references/roussou2019/checked")
        text = client.get(f"/api/projects/{pid}/files/refs.bib").text
        assert text.count("meertex-checked") == 1
        r = client.delete(f"/api/projects/{pid}/references/roussou2019/checked")
        assert r.status_code == 200
        assert r.json()["mark"] is None
        assert "meertex-checked" not in client.get(f"/api/projects/{pid}/files/refs.bib").text


def test_take_a_value_and_the_editor_problems(fake):
    with running_app() as client:
        _me, pid = _setup(client)
        _run(client, pid)
        r = client.post(f"/api/projects/{pid}/references/roussou2019/fields",
                        json={"field": "author", "value": "Roussou, Maria and Perry, Sara and Katifori, Akrivi"})
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["fields"]["author"] == "Roussou, Maria and Perry, Sara and Katifori, Akrivi"
        # The entry changed, so it waits for the next run.
        assert entry["status"] == "pending"
        r = client.post(f"/api/projects/{pid}/references/roussou2019/fields", json={"field": "pages", "value": "1--13"})
        assert "pages = {1--13}," in client.get(f"/api/projects/{pid}/files/refs.bib").text
        assert client.post(f"/api/projects/{pid}/references/roussou2019/fields",
                           json={"field": "title", "value": "{unbalanced"}).status_code == 422
        assert client.post(f"/api/projects/{pid}/references/nope/fields",
                           json={"field": "title", "value": "x"}).status_code == 404

        problems = client.get(f"/api/projects/{pid}/references/problems").json()
        by_message = {p["message"]: p for p in problems}
        # The new pages field moved everything after it down a line.
        assert any(p["path"] == "refs.bib" and p["level"] == "error" and p["line"] == 30 for p in problems)
        missing = by_message["There is no entry nothere in the .bib files."]
        assert (missing["path"], missing["line"], missing["level"]) == ("main.tex", 4, "warning")
        doi = next(p for p in problems if p["message"].startswith("fake2023: doi.org does not know"))
        assert doi["line"] == 15  # the doi field's line


def test_duplicate_keys_are_409_on_writes():
    with running_app() as client:
        _me, pid = _setup(client)
        twice = BIB + "\n@misc{gmt, title = {Again}}\n@inproceedings{copy, title = {Other}, doi = {10.1145/3290605.3300857}}\n"
        client.put(f"/api/projects/{pid}/files/more.bib", content=twice.encode())
        entries = client.get(f"/api/projects/{pid}/references").json()["entries"]
        assert sum(1 for e in entries if e["key"] == "gmt") == 3
        assert any(f["code"] == "duplicate_key" for e in entries if e["key"] == "gmt" for f in e["findings"])
        dup = next(f for e in entries if e["key"] == "copy" for f in e["findings"] if f["code"] == "duplicate")
        assert dup["message"] == "roussou2019 is the same work, it seems."
        assert dup["record_value"] is None
        assert client.post(f"/api/projects/{pid}/references/gmt/checked").status_code == 409


def test_adding_entries(fake):
    fake.answers["doi"]["10.48550/arxiv.1706.03762"] = found(rec(
        "doi.org", doi="10.48550/arXiv.1706.03762", title="Attention Is All You Need",
        people=["Ashish Vaswani", "Noam Shazeer"], years=[2017], arxiv="1706.03762", publisher="arXiv"))
    with running_app() as client:
        _me, pid = _setup(client)
        base = f"/api/projects/{pid}/references"
        r = client.post(base, json={"source": "https://doi.org/10.1145/3290605.3300857", "dry_run": True})
        assert r.status_code == 200, r.text
        dry = r.json()
        assert dry["key"] == "roussou2019transformation"
        assert dry["path"] == "refs.bib"
        assert "@inproceedings{roussou2019transformation," in dry["bibtex"]
        assert "  pages = {1--13}," in dry["bibtex"]
        assert "roussou2019transformation" not in client.get(f"/api/projects/{pid}/files/refs.bib").text

        r = client.post(base, json={"source": "arXiv:1706.03762"})
        assert r.status_code == 201, r.text
        added = r.json()
        assert added["key"] == "vaswani2017attention"
        text = client.get(f"/api/projects/{pid}/files/refs.bib").text
        assert text.endswith(added["bibtex"])
        assert text.splitlines()[added["line"] - 1] == "@misc{vaswani2017attention,"
        assert "  eprint = {1706.03762}," in added["bibtex"]

        again = client.post(base, json={"source": "1706.03762"}).json()
        assert again["key"] == "vaswani2017attentionb"
        assert client.post(base, json={"source": "1706.03762", "key": "Vaswani2017Attention"}).status_code == 422
        assert client.post(base, json={"source": "10.1145/0000000.0000001"}).status_code == 404
        assert client.post(base, json={"source": "no idea"}).status_code == 422
        r = client.post(base, json={"source": "10.1145/3290605.3300857", "path": "extra.bib", "key": "mine"})
        assert r.status_code == 201
        assert client.get(f"/api/projects/{pid}/files/extra.bib").text.startswith("@inproceedings{mine,")


def test_roles_switches_and_restarts(monkeypatch, fake):
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import ReferenceRun

    with running_app() as client:
        me, pid = _setup(client)
        viewer_id, _email, token = make_user()
        from app.models import Membership

        with SessionLocal() as db:
            db.add(Membership(project_id=pid, user_id=viewer_id, role="viewer"))
            db.commit()
        other = bare_client()
        auth = {"Authorization": f"Bearer {token}"}
        assert other.get(f"/api/projects/{pid}/references", headers=auth).status_code == 200
        assert other.post(f"/api/projects/{pid}/references/check", headers=auth, json={}).status_code == 403
        assert other.post(f"/api/projects/{pid}/references/gmt/checked", headers=auth).status_code == 403

        assert client.post(f"/api/projects/{pid}/references/check", json={"claims": True}).status_code == 503
        monkeypatch.setattr(get_settings(), "references_network", False)
        assert client.post(f"/api/projects/{pid}/references/check", json={}).status_code == 503
        assert client.post(f"/api/projects/{pid}/references", json={"source": "10.1/x"}).status_code == 503
        monkeypatch.setattr(get_settings(), "references_network", True)

        # A run the database calls running, but no thread works on, was cut
        # off by a restart.
        with SessionLocal() as db:
            db.add(ReferenceRun(id="00000000-0000-4000-8000-000000000001", project_id=pid, status="running",
                                created_by=me["id"]))
            db.commit()
        run = client.get(f"/api/projects/{pid}/references/check").json()
        assert run["status"] == "failed"
        assert run["error"] == "The server restarted during the check."
        assert client.get(f"/api/projects/{pid}/references/check/not-a-uuid").status_code == 404


def test_claims(monkeypatch, fake):
    from app.config import get_settings
    from app.references import claims

    asked = []

    def ask(content: str) -> list[dict]:
        asked.append(content)
        return [{"index": 1, "verdict": "not_supported", "reason": "The paper is about museums.",
                 "quote": "We provoke museum visitors."}]

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(claims, "ask_claude", ask)
    with running_app() as client:
        _me, pid = _setup(client)
        assert client.get(f"/api/projects/{pid}/references").json()["claims_enabled"] is True
        run = _run(client, pid, claims=True)
        assert run["status"] == "done"
        assert len(asked) == 1
        assert "Provocation changes visitors [roussou2019]." in asked[0]
        assert "We provoke museum visitors." in asked[0]
        entries = _entries(client, pid)
        claim = next(f for f in entries["roussou2019"]["findings"] if f["code"] == "claim")
        assert claim["verdict"] == "not_supported"
        assert claim["level"] == "warning"
        assert (claim["path"], claim["line"]) == ("main.tex", 3)
        assert claim["record_value"] == "We provoke museum visitors."
        assert entries["roussou2019"]["claims_at"] is not None
        # fake2023 has no record and so no abstract: its sentence is listed only.
        no_source = [f for f in entries["fake2023"]["findings"] if f["code"] == "claim"]
        assert [f["verdict"] for f in no_source] == ["no_source"]

        problems = client.get(f"/api/projects/{pid}/references/problems").json()
        assert any(p["path"] == "main.tex" and p["line"] == 3 for p in problems)

        # The same question again is answered from the cache.
        _run(client, pid, claims=True)
        assert len(asked) == 1
