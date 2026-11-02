"""The example paper a new account starts with (docs/DESIGN.md §19)."""

import pytest
from compile_helpers import TOKEN, compile_service, needs_tex
from helpers import needs_db, running_app, second_client, sign_in
from test_live_helpers import wait_for

pytestmark = needs_db

JONAS = "jonas.lindqvist@example.org"
AMARA = "amara.okafor@example.org"


@pytest.fixture(autouse=True)
def example_on(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "example_paper", True)
    monkeypatch.setattr(get_settings(), "references_network", False)


def _example(client) -> dict:
    papers = client.get("/api/projects").json()
    assert [p["name"] for p in papers] == ["example-paper"]
    return client.get(f"/api/projects/{papers[0]['id']}").json()


def _file(client, pid: str, ref: str = "live", path: str = "main.tex") -> str:
    r = client.get(f"/api/projects/{pid}/files/{path}", params={"ref": ref})
    assert r.status_code == 200, r.text
    return r.text


def test_a_first_sign_in_makes_the_example():
    with running_app() as client:
        sign_in(client, "new@example.com")
        p = _example(client)
        assert p["title"] == "The Whole Life of a Paper, Shown on an Example"
        assert p["labels"] == ["example"]
        assert p["outcome"] is None
        assert [(m["email"], m["role"]) for m in p["members"]] == [
            ("new@example.com", "owner"), (JONAS, "editor"), (AMARA, "reviewer"),
        ]
        # No reminder mails about a made-up paper, until the owner lifts the mute.
        assert p["reminders_muted"] is True

        dates = {d["kind"]: d for d in p["dates"]}
        assert len(dates) == 8
        assert dates["submission"]["answer"] == "yes"
        assert len(dates["submission"]["changes"]) == 1
        assert dates["abstract"]["answer"] == "yes"
        assert [q["kind"] for q in p["questions"]] == ["rebuttal_end"]
        assert p["next_date"]["kind"] == "notification"

        versions = {v["tag"]: v for v in p["versions"]}
        assert list(versions) == ["submission", "arxiv"]
        assert versions["submission"]["date_id"] == dates["submission"]["id"]

        history = client.get(f"/api/projects/{p['id']}/history").json()
        assert [c["message"].splitlines()[0] for c in history][-1] == "Project created"
        assert len(history) == 6
        times = [c["at"] for c in history]
        assert times == sorted(times, reverse=True)
        assert {e for c in history for e in c["editors"]} == {JONAS, AMARA}
        assert versions["submission"]["commit"] in {c["commit"] for c in history}


def test_the_versions_hold_their_own_text():
    with running_app() as client:
        sign_in(client, "new@example.com")
        pid = _example(client)["id"]
        submission = _file(client, pid, "submission")
        arxiv = _file(client, pid, "arxiv")
        now = _file(client, pid)
        assert "\\author{Anonymous Authors}" in submission
        assert "\\author{Jonas Lindqvist \\and You \\and Amara Okafor}" in arxiv
        assert "Anywhere on Earth" in now and "Anywhere on Earth" not in arxiv
        assert "meertex checkout example-paper" in now
        # Jonas added the issue number after Amara had checked the entry.
        assert "number  = {1}" in _file(client, pid, "submission", "refs.bib")
        assert "number  = {1}" not in _file(client, pid, "HEAD~3", "refs.bib")


def test_the_review_has_a_thread_in_every_state():
    with running_app() as client:
        me = sign_in(client, "new@example.com")
        p = _example(client)
        url = f"/api/projects/{p['id']}"
        reviews = client.get(f"{url}/reviews").json()
        assert [(r["reviewer"]["email"], r["state"], r["verdict"]) for r in reviews] == [
            (AMARA, "submitted", "changes_requested"),
        ]
        assert reviews[0]["version"]["tag"] == "submission"

        threads = client.get(f"{url}/threads", params={"state": "all"}).json()
        assert sorted(t["state"] for t in threads) == ["accepted", "open", "open", "open", "resolved"]
        assert not any(t["outdated"] for t in threads)
        accepted = next(t for t in threads if t["state"] == "accepted")
        assert accepted["changed"] and "+Anywhere on Earth," in accepted["resolution"]["diff"]
        single = next(t for t in threads if t["review_id"] is None)
        assert [m["email"] for m in single["comments"][0]["mentions"]] == [JONAS]
        assert single["text"] == "meertex checkout example-paper"

        # The owner accepts what Jonas resolved and applies Amara's suggestion.
        resolved = next(t for t in threads if t["state"] == "resolved")
        assert client.post(f"{url}/threads/{resolved['id']}/accept", json={}).json()["state"] == "accepted"
        suggested = next(t for t in threads if any(c["suggestion"] for c in t["comments"]))
        assert client.post(f"{url}/threads/{suggested['id']}/apply", json={}).json()["state"] == "resolved"
        assert "as a latexdiff\nPDF that marks every change." in _file(client, p["id"])

        # Jonas asked the owner for a review, which they can start.
        requests = client.get(f"{url}/review-requests").json()
        assert [(r["reviewer"]["id"], r["requested_by"]["email"], r["state"]) for r in requests] == [
            (me["id"], JONAS, "open"),
        ]
        r = client.post(f"{url}/review-requests/{requests[0]['id']}/start")
        assert r.status_code == 201, r.text


def test_the_bibliography_has_its_mistakes():
    """Without the network only the checks that need none run. The others
    are in the paper's Table 1 and were tried against the live services."""
    with running_app() as client:
        sign_in(client, "new@example.com")
        refs = client.get(f"/api/projects/{_example(client)['id']}/references").json()
        status = {e["key"]: e["status"] for e in refs["entries"]}
        assert status["shapiro2011crdt"] == "checked"
        assert status["wilson2014best"] == "changed"
        assert status["ellis1989concurrency"] == "error"
        assert [m["key"] for m in refs["missing"]] == ["mittelbach2004companion"]
        knuth = next(e for e in refs["entries"] if e["key"] == "knuth1984texbook")
        assert [f["code"] for f in knuth["findings"]] == ["not_cited"]
        marks = {e["key"]: e["mark"]["by"] for e in refs["entries"] if e["mark"]}
        assert marks == {"shapiro2011crdt": "Jonas Lindqvist", "wilson2014best": "Amara Okafor"}


def test_the_reference_check_starts_with_the_network(monkeypatch):
    from app.config import get_settings
    from app.example import build

    started = []
    monkeypatch.setattr(get_settings(), "references_network", True)
    monkeypatch.setattr(build.references, "start", lambda db, project, user, **kw: started.append((user.email, kw)))
    with running_app() as client:
        sign_in(client, "new@example.com")
        assert started == [(JONAS, {"want_claims": False, "refresh": False})]


def test_only_a_first_sign_in_makes_one():
    with running_app() as client:
        sign_in(client, "new@example.com")
        sign_in(client, "new@example.com")
        _example(client)
        with second_client("other@example.com") as other:
            _example(other)
        from app.database import SessionLocal
        from app.models import User

        with SessionLocal() as db:
            # The made-up coauthors are shared by every example.
            assert db.query(User).filter(User.email.in_([JONAS, AMARA])).count() == 2
            jonas = db.query(User).filter(User.email == JONAS).one()
            assert (jonas.name, jonas.reminders, jonas.mention_mails) == ("Jonas Lindqvist", False, False)


def test_an_invited_person_gets_one_too():
    with running_app() as client:
        sign_in(client, "owner@example.com")
        pid = _example(client)["id"]
        assert client.post(f"/api/projects/{pid}/members", json={"email": "arne@example.com", "role": "editor"}
                           ).status_code in (200, 201)
        with second_client("arne@example.com") as arne:
            names = sorted(p["name"] for p in arne.get("/api/projects").json())
            assert names == ["example-paper", "example-paper"]


def test_switched_off_makes_none(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "example_paper", False)
    with running_app() as client:
        sign_in(client, "new@example.com")
        assert client.get("/api/projects").json() == []


def test_a_failure_leaves_nothing_and_signs_in(monkeypatch):
    from app.config import get_settings
    from app.example import build

    def broken(*args, **kwargs):
        raise RuntimeError("no example today")

    monkeypatch.setattr(build, "_fill", broken)
    with running_app() as client:
        assert sign_in(client, "new@example.com")["email"] == "new@example.com"
        assert client.get("/api/projects").json() == []
        assert list(get_settings().projects_dir.iterdir()) == []


def test_the_made_up_coauthors_get_no_mail(capsys):
    from app import emailer

    emailer.send_email(JONAS, "Hello", "Anybody there?")
    emailer.send_email(AMARA.upper(), "Hello", "Anybody there?")
    assert "[EMAIL]" not in capsys.readouterr().out
    emailer.send_email("new@example.com", "Hello", "Somebody is.")
    assert "To: new@example.com" in capsys.readouterr().out


def test_the_dashboard_leaves_the_examples_out(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "instance_admins", "new@example.com")
    with running_app() as client:
        sign_in(client, "new@example.com")
        _example(client)
        data = client.get("/api/admin/instance").json()
        assert data["totals"]["accounts"] == 1
        assert data["totals"]["waiting"] == 0
        assert data["totals"]["papers"] == 0
        assert [a["email"] for a in data["accounts"]] == ["new@example.com"]
        assert data["accounts"][0]["papers"] == 0
        for name in ("papers", "snapshots", "comments", "reviews", "versions"):
            assert sum(data["series"][name]) == 0, name


@needs_tex
def test_the_paper_and_its_versions_compile(monkeypatch, tmp_path):
    from app.config import get_settings

    with compile_service(tmp_path) as service:
        monkeypatch.setattr(get_settings(), "compile_url", service.url)
        monkeypatch.setattr(get_settings(), "compile_token", TOKEN)
        with running_app() as client:
            sign_in(client, "new@example.com")
            pid = _example(client)["id"]

            def states():
                versions = client.get(f"/api/projects/{pid}").json()["versions"]
                return [(v.get("compiled") or {}).get("status") for v in versions]

            assert wait_for(lambda: "running" not in states(), timeout=120)
            assert states() == ["success", "success"]
            r = client.post(f"/api/projects/{pid}/compile")
            assert r.status_code == 200, r.text
            live = r.json()
            assert live["status"] == "success", live["problems"]
            # The citation without an entry is on purpose (Table 1).
            errors = [x for x in live["problems"] if x["level"] == "error"]
            assert errors == []
