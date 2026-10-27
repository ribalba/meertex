"""Projects: create, read, edit, list, fork, delete (DESIGN §4, §11), and one
paper's whole life from the first deadline to the resubmission."""

import time

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db

ICSE = {
    "title": "Towards Green LLMs: Energy-Aware Inference",
    "conference": "ICSE",
    "conference_name": "International Conference on Software Engineering",
    "year": 2027,
    "track": "Research Track",
    "labels": [" green-software ", "", "with-arne", "Green-Software"],
}

SUMMARY_KEYS = {
    "id", "name", "title", "paper", "conference", "year", "track", "outcome", "labels", "archived", "role",
    "folder_id", "folder_path", "next_date", "question_count", "updated_at", "url",
    "planned", "open_threads", "writing_days", "own_writing_days", "writing", "reminders_muted",
}
DETAIL_KEYS = SUMMARY_KEYS | {
    "conference_name", "cfp_url", "conference_url", "submission_url", "location", "publisher", "page_limit",
    "template", "review_model", "notes", "main_file", "outcome_at", "doi", "publication_url", "forked_from_id",
    "owner_id", "created_at", "owner", "dates", "questions", "versions", "members", "forked_from", "file_count",
    "compiler",
}


def _edit(project_id: str, text: str) -> str:
    """Write main.tex in the live document and snapshot it. Returns the commit."""
    from app import live
    from app.live import rooms, ydoc

    with rooms.get(project_id).edit("didi@example.com") as doc:
        ydoc.put_text(doc, "main.tex", text)
    return live.snapshot(project_id)


def _iso(dt) -> str:
    return dt.isoformat() + "Z"


def test_create_and_read():
    with running_app() as client:
        me = sign_in(client)
        r = client.post("/api/projects", json=ICSE | {"starter": "basic", "notes": "# CFP\n\n10 pages"})
        assert r.status_code == 201, r.text
        p = r.json()
        assert set(p) == DETAIL_KEYS
        assert p["name"] == "2027-icse-green-llms-energy-aware"
        assert p["paper"] == "green-llms-energy-aware"
        assert p["labels"] == ["green-software", "with-arne"]
        assert p["role"] == "owner"
        assert p["url"] == "http://testserver/p/2027-icse-green-llms-energy-aware"
        assert p["file_count"] == 2
        assert p["template"] == ""
        assert p["main_file"] == "main.tex"
        assert p["outcome"] is None and p["outcome_at"] is None
        assert p["owner"] == {"id": me["id"], "email": me["email"], "name": None, "avatar_url": None}
        assert p["members"][0]["role"] == "owner"
        assert p["dates"] == [] and p["questions"] == [] and p["versions"] == []
        assert p["forked_from"] is None
        assert p["created_at"].endswith("Z")

        by_name = client.get(f"/api/projects/{p['name']}").json()
        assert by_name["id"] == p["id"]
        assert client.get(f"/api/projects/{p['id']}").json()["name"] == p["name"]

        # The template is the LaTeX class the venue wants. The project starts
        # without files unless a starter says otherwise.
        q = client.post("/api/projects", json={"name": "llncs-paper", "template": "llncs"}).json()
        assert q["template"] == "llncs"
        assert q["file_count"] == 0


def test_strangers_get_404_and_low_roles_403():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json=ICSE).json()
        with second_client("eve@example.com") as eve:
            assert eve.get(f"/api/projects/{p['id']}").status_code == 404
            assert eve.get(f"/api/projects/{p['name']}").status_code == 404
            assert eve.patch(f"/api/projects/{p['id']}", json={"title": "x"}).status_code == 404
            assert eve.delete(f"/api/projects/{p['id']}", params={"confirm": p["name"]}).status_code == 404
            assert eve.post(f"/api/projects/{p['id']}/fork", json={}).status_code == 404
            assert eve.get("/api/projects").json() == []
        client.post(f"/api/projects/{p['id']}/members", json={"email": "vic@example.com", "role": "viewer"})
        with second_client("vic@example.com") as vic:
            assert vic.get(f"/api/projects/{p['name']}").json()["role"] == "viewer"
            assert vic.patch(f"/api/projects/{p['id']}", json={"title": "x"}).status_code == 403
            assert vic.post(f"/api/projects/{p['id']}/fork", json={}).status_code == 403
            assert client.get("/api/auth/me").status_code == 200


def test_patch_fields_outcome_and_rename():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json=ICSE).json()
        other = client.post("/api/projects", json={"name": "taken"}).json()
        r = client.patch(
            f"/api/projects/{p['id']}",
            json={"location": " Rio de Janeiro, Brazil ", "page_limit": "10 + 2", "track": None, "labels": ["a", "a"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["location"] == "Rio de Janeiro, Brazil"
        assert body["track"] == ""
        assert body["labels"] == ["a"]
        # The name does not follow the fields.
        assert body["name"] == p["name"]

        body = client.patch(f"/api/projects/{p['id']}", json={"outcome": "accepted"}).json()
        assert body["outcome"] == "accepted"
        assert body["outcome_at"].endswith("Z")
        body = client.patch(f"/api/projects/{p['id']}", json={"outcome": None}).json()
        assert body["outcome"] is None and body["outcome_at"] is None
        assert client.patch(f"/api/projects/{p['id']}", json={"outcome": "maybe"}).status_code == 422

        assert client.patch(f"/api/projects/{p['id']}", json={"name": other["name"]}).status_code == 409
        assert client.patch(f"/api/projects/{p['id']}", json={"name": "no spaces"}).status_code == 422
        body = client.patch(f"/api/projects/{p['id']}", json={"name": "green-llm"}).json()
        assert body["name"] == "green-llm"
        assert body["url"].endswith("/p/green-llm")
        assert client.get("/api/projects/green-llm").status_code == 200


def test_list_filters_and_order():
    with running_app() as client:
        sign_in(client)
        soon = client.post(
            "/api/projects",
            json={"name": "soon", "conference": "ICSE", "year": 2027, "dates": [{"kind": "submission", "date": "2099-01-10"}]},
        ).json()
        later = client.post(
            "/api/projects",
            json={"name": "later", "conference": "FSE", "year": 2027, "dates": [{"kind": "submission", "date": "2099-06-10"}]},
        ).json()
        old = client.post("/api/projects", json={"name": "old", "conference": "icse", "year": 2026, "labels": ["green"]}).json()
        fresh = client.post("/api/projects", json={"name": "fresh", "notes": "about 100% energy_use"}).json()
        client.patch(f"/api/projects/{old['id']}", json={"outcome": "accepted"})
        client.patch(f"/api/projects/{fresh['id']}", json={"archived": True})

        def names(**params) -> list[str]:
            r = client.get("/api/projects", params=params)
            assert r.status_code == 200, r.text
            return [s["name"] for s in r.json()]

        # Next date first, soonest on top, then the most recently updated.
        assert names() == ["soon", "later", "old"]
        assert names(archived="all") == ["soon", "later", "fresh", "old"]
        assert names(archived="true") == ["fresh"]
        assert names(outcome="accepted") == ["old"]
        assert names(outcome="none") == ["soon", "later"]
        assert names(outcome="accepted,none") == ["soon", "later", "old"]
        assert client.get("/api/projects", params={"outcome": "great"}).status_code == 422
        assert names(conference="ICSE") == ["soon", "old"]
        assert names(year=2026) == ["old"]
        assert names(label="GREEN") == ["old"]
        assert names(q="fs") == ["later"]
        assert names(q="100%", archived="all") == ["fresh"]
        assert names(q="r_y", archived="all") == []
        summary = client.get("/api/projects").json()[0]
        assert set(summary) == SUMMARY_KEYS
        assert summary["next_date"]["kind"] == "submission"
        assert summary["next_date"]["seconds_left"] > 0
        assert soon["id"] != later["id"]


def test_delete_needs_the_owner_and_the_name():
    from app.config import get_settings

    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json=ICSE | {"starter": "basic"}).json()
        client.post(f"/api/projects/{p['id']}/members", json={"email": "ed@example.com", "role": "editor"})
        with second_client("ed@example.com") as ed:
            assert ed.delete(f"/api/projects/{p['id']}", params={"confirm": p["name"]}).status_code == 403
        assert client.delete(f"/api/projects/{p['id']}").status_code == 422
        assert client.delete(f"/api/projects/{p['id']}", params={"confirm": "wrong"}).status_code == 422
        assert client.delete(f"/api/projects/{p['id']}", params={"confirm": p["name"]}).status_code == 200
        assert client.get(f"/api/projects/{p['id']}").status_code == 404
        assert not (get_settings().projects_dir / p["id"]).exists()


def test_failed_files_leave_nothing_behind(monkeypatch):
    from app import live
    from app.database import SessionLocal
    from app.models import Project

    def broken(project_id: str, template: str = "empty") -> str:
        raise RuntimeError("disk full")

    with running_app() as client:
        sign_in(client)
        monkeypatch.setattr(live, "init_project", broken)
        r = client.post("/api/projects", json=ICSE | {"folder": "2027"})
        assert r.status_code == 500
        with SessionLocal() as db:
            assert db.query(Project).count() == 0
        assert client.get("/api/projects").json() == []


def test_a_papers_life():
    """Dates in the past and the future, the questions they raise, handing in
    late with the deadline's state, the decision, a moved date, a fork and the
    end."""
    from datetime import timedelta

    from app import live
    from app.models import utcnow

    with running_app() as client:
        sign_in(client)
        r = client.post(
            "/api/projects",
            json=ICSE
            | {
                "starter": "basic",
                "dates": [
                    {"kind": "abstract", "date": "2026-01-10"},
                    {"kind": "submission", "date": "2099-01-15"},
                    {"kind": "notification", "date": "2099-03-01", "tz": "UTC"},
                    {"kind": "camera_ready", "date": "2099-02-01"},
                    {"kind": "conference_start", "date": "2099-06-01"},
                ],
            },
        )
        assert r.status_code == 201, r.text
        p = r.json()
        pid = p["id"]
        dates = {d["kind"]: d for d in p["dates"]}

        # Camera-ready is earlier here, but it only counts once accepted.
        assert p["next_date"]["kind"] == "submission"
        assert p["next_date"]["due_at"] == "2099-01-16T11:59:59Z"
        assert p["question_count"] == 1
        [question] = p["questions"]
        assert question["kind"] == "abstract"
        assert question["text"] == "The abstract registration deadline passed on 10 Jan 2026. Did you register the abstract?"
        assert question["answers"] == ["yes", "no", "moved"]
        p = client.post(f"/api/projects/{pid}/dates/{dates['abstract']['id']}/answer", json={"answer": "yes"}).json()
        assert p["questions"] == []

        # Work goes on until the deadline, which then passes, and on after it.
        submitted = _edit(pid, "the submitted text")
        deadline = utcnow()
        r = client.patch(f"/api/projects/{pid}/dates/{dates['submission']['id']}", json={"due_at": _iso(deadline)})
        assert r.status_code == 200
        moved = r.json()
        assert moved["date_only"] is False
        assert moved["passed"] is True
        assert [(c["from"], c["by"]) for c in moved["changes"]] == [("2099-01-16T11:59:59Z", "didi@example.com")]
        time.sleep(1.1)  # git dates have whole seconds
        after = _edit(pid, "edited after the deadline")
        assert after != submitted

        p = client.get(f"/api/projects/{pid}").json()
        [question] = p["questions"]
        assert question["kind"] == "submission"
        assert question["text"].startswith("The paper submission deadline passed on ")
        assert question["text"].endswith(" AoE. Did you hand it in?")
        # Rebuttal and notification wait until it was handed in.
        p = client.post(
            f"/api/projects/{pid}/dates/{dates['submission']['id']}/answer",
            json={"answer": "yes", "version": {"note": "as uploaded to HotCRP"}},
        ).json()
        [version] = p["versions"]
        assert version["kind"] == "submission"
        assert version["tag"] == "submission"
        assert version["label"] == "Submission"
        assert version["commit"] == submitted
        assert version["date_id"] == dates["submission"]["id"]
        assert version["note"] == "as uploaded to HotCRP"
        assert version["created_by"]["email"] == "didi@example.com"
        assert live.resolve_commit(pid, "submission") == submitted

        # The notification comes early.
        r = client.patch(f"/api/projects/{pid}/dates/{dates['notification']['id']}", json={"date": "2026-03-01"})
        assert r.json()["changes"][0]["to"] == "2026-03-01T23:59:59Z"
        p = client.get(f"/api/projects/{pid}").json()
        [question] = p["questions"]
        assert question["text"] == "The notification date passed on 1 Mar 2026. Was the paper accepted?"
        assert question["answers"] == ["accepted", "rejected", "revision", "moved"]
        p = client.post(
            f"/api/projects/{pid}/dates/{dates['notification']['id']}/answer", json={"answer": "accepted"}
        ).json()
        assert p["outcome"] == "accepted"
        assert p["outcome_at"].endswith("Z")
        assert p["questions"] == []
        assert p["next_date"]["kind"] == "camera_ready"

        accepted = client.get("/api/projects", params={"outcome": "accepted"}).json()
        assert [s["id"] for s in accepted] == [pid]
        assert client.get("/api/projects", params={"outcome": "rejected"}).json() == []

        # Camera-ready moves, which is recorded.
        r = client.patch(f"/api/projects/{pid}/dates/{dates['camera_ready']['id']}", json={"date": "2099-02-08"})
        changes = r.json()["changes"]
        assert len(changes) == 1
        assert changes[0]["from"] == "2099-02-02T11:59:59Z"
        assert changes[0]["to"] == "2099-02-09T11:59:59Z"

        # A resubmission elsewhere starts from the live files, not the dates.
        r = client.post(
            f"/api/projects/{pid}/fork",
            json={"conference": "FSE", "year": 2028, "dates": [{"kind": "submission", "date": "2099-09-01"}]},
        )
        assert r.status_code == 201, r.text
        fork = r.json()
        assert fork["name"] == "2028-fse-green-llms-energy-aware"
        assert fork["title"] == ICSE["title"]
        assert fork["labels"] == ["green-software", "with-arne"]
        assert fork["forked_from"] == {"id": pid, "name": p["name"]}
        assert fork["outcome"] is None
        assert fork["versions"] == []
        assert [d["kind"] for d in fork["dates"]] == ["submission"]
        assert fork["file_count"] == p["file_count"] == 2
        assert fork["track"] == ""
        from app.live import git

        assert git.run(fork["id"], "log", "-1", "--format=%s").decode().strip() == f"Resubmission of {p['name']}"
        assert git.show(fork["id"], "HEAD", "main.tex") == b"edited after the deadline"

        # The old paper stays accepted. Then it goes.
        assert client.delete(f"/api/projects/{pid}", params={"confirm": p["name"]}).status_code == 200
        assert client.get(f"/api/projects/{pid}").status_code == 404
        assert client.get(f"/api/projects/{fork['id']}").json()["forked_from"] is None
        assert utcnow() - deadline < timedelta(minutes=5)


def test_fork_lands_next_to_the_source():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json=ICSE | {"folder": "2027/Rejected"}).json()
        fork = client.post(f"/api/projects/{p['id']}/fork", json={"conference": "ICSE", "year": 2028}).json()
        assert fork["folder_path"] == "2027/Rejected"
        elsewhere = client.post(
            f"/api/projects/{p['id']}/fork", json={"conference": "ICSE", "year": 2028, "folder": "2028"}
        ).json()
        assert elsewhere["name"] == "2028-icse-green-llms-energy-aware-2"
        assert elsewhere["folder_path"] == "2028"
        r = client.post(f"/api/projects/{p['id']}/fork", json={"conference": "ASE", "year": 2028, "dry_run": True})
        assert r.json() == {"name": "2028-ase-green-llms-energy-aware", "dry_run": True}


def test_a_fork_keeps_the_compiler():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "xe", "compiler": "xelatex"}).json()
        fork = client.post(f"/api/projects/{p['id']}/fork", json={"conference": "FSE", "year": 2028}).json()
        assert fork["compiler"] == "xelatex"
