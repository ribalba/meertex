"""Planned papers, writing time and the timeline (docs/DESIGN.md §3, §4, §5)."""

from datetime import datetime, timedelta

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_a_plan_needs_no_title_and_asks_nothing():
    with running_app() as client:
        sign_in(client)
        r = client.post("/api/projects", json={
            "planned": True, "conference": "ICT4S", "year": 2027,
            "dates": [{"kind": "submission", "date": "2020-01-15"}, {"kind": "notification", "date": "2099-03-01"}],
        })
        assert r.status_code == 201, r.text
        plan = r.json()
        assert plan["planned"] is True and plan["title"] == ""
        assert plan["name"] == "2027-ict4s"
        # The deadline passed, but there is no paper to ask about.
        assert plan["question_count"] == 0 and plan["questions"] == []
        assert [p["name"] for p in client.get("/api/projects", params={"planned": "true"}).json()] == ["2027-ict4s"]
        assert client.get("/api/projects", params={"planned": "false"}).json() == []


def test_starting_a_plan_names_it_and_writes_starter_files():
    with running_app() as client:
        sign_in(client)
        plan = client.post("/api/projects", json={"planned": True, "conference": "ICT4S", "year": 2027}).json()
        r = client.post(f"/api/projects/{plan['id']}/start", json={"title": "Green ICT for small labs", "starter": "basic"})
        assert r.status_code == 200, r.text
        paper = r.json()
        assert paper["planned"] is False
        assert paper["name"] == "2027-ict4s-green-ict-small"
        assert paper["file_count"] == 2
        assert client.post(f"/api/projects/{plan['id']}/start", json={"title": "Again"}).status_code == 422

        other = client.post("/api/projects", json={"planned": True, "conference": "LIMITS", "year": 2027}).json()
        kept = client.post(f"/api/projects/{other['id']}/start", json={"title": "Something", "keep_name": True}).json()
        assert kept["name"] == "2027-limits" and kept["file_count"] == 0


def test_viewers_cannot_start_a_plan():
    with running_app() as client:
        sign_in(client)
        plan = client.post("/api/projects", json={"planned": True, "conference": "ICT4S", "year": 2027}).json()
        client.post(f"/api/projects/{plan['id']}/members", json={"email": "v@example.com", "role": "viewer"})
        with second_client("v@example.com") as viewer:
            assert viewer.post(f"/api/projects/{plan['id']}/start", json={"title": "Mine"}).status_code == 403


def test_the_writing_window_follows_the_writing_time():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={
            "name": "w", "dates": [{"kind": "submission", "due_at": "2099-02-01T12:00", "tz": "UTC"}],
        }).json()
        url = f"/api/projects/{p['id']}"
        w = p["writing"]
        assert (w["days"], w["own"], p["writing_days"]) == (30, False, 30)
        assert _at(w["end"]) - _at(w["start"]) == timedelta(days=30)
        assert w["date_id"] == p["dates"][0]["id"]

        assert client.patch("/api/auth/me", json={"writing_days": 45}).json()["writing_days"] == 45
        assert client.get(url).json()["writing"]["days"] == 45
        assert client.patch("/api/auth/me", json={"writing_days": 0}).status_code == 422

        own = client.patch(url, json={"writing_days": 10}).json()
        assert (own["writing"]["days"], own["writing"]["own"], own["writing_days"]) == (10, True, 10)
        back = client.patch(url, json={"writing_days": None}).json()
        assert (back["writing"]["days"], back["writing"]["own"]) == (45, False)


def test_the_window_moves_to_the_revision_and_ends_with_a_decision():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "r", "dates": [
            {"kind": "submission", "date": "2020-01-01"},
            {"kind": "revision", "date": "2099-06-01", "tz": "UTC"},
        ]}).json()
        url = f"/api/projects/{p['id']}"
        submission = next(d for d in p["dates"] if d["kind"] == "submission")
        revision = next(d for d in p["dates"] if d["kind"] == "revision")
        assert p["writing"]["date_id"] == submission["id"]
        client.post(f"{url}/dates/{submission['id']}/answer", json={"answer": "yes"})
        assert client.get(url).json()["writing"] is None
        assert client.patch(url, json={"outcome": "revision"}).json()["writing"]["date_id"] == revision["id"]
        assert client.patch(url, json={"outcome": "accepted"}).json()["writing"] is None


def test_the_timeline_lists_every_paper_with_its_dates():
    with running_app() as client:
        sign_in(client)
        client.post("/api/projects", json={"name": "late", "dates": [{"kind": "submission", "date": "2099-09-01"}]})
        client.post("/api/projects", json={"name": "early", "dates": [{"kind": "submission", "date": "2099-03-01"}]})
        client.post("/api/projects", json={"planned": True, "conference": "ICT4S", "year": 2099})
        client.post("/api/projects", json={"name": "gone", "archived": True})
        t = client.get("/api/timeline").json()
        assert t["writing_days"] == 30 and t["today"].endswith("Z")
        assert [p["name"] for p in t["projects"]] == ["early", "late", "2099-ict4s"]
        assert t["projects"][0]["dates"][0]["kind"] == "submission"
        assert "gone" in [p["name"] for p in client.get("/api/timeline", params={"archived": "all"}).json()["projects"]]


def test_open_threads_count_what_you_can_see():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "talk", "starter": "basic"}).json()
        url = f"/api/projects/{p['id']}"
        client.post(f"{url}/members", json={"email": "rev@example.com", "role": "reviewer"})
        r = client.post(f"{url}/threads", json={"path": "main.tex", "line_start": 1, "body": "Why article?"})
        assert r.status_code == 201, r.text
        assert client.get(url).json()["open_threads"] == 1
        with second_client("rev@example.com") as rev:
            review = rev.post(f"{url}/reviews", json={}).json()
            pending = rev.post(f"{url}/threads", json={"path": "main.tex", "line_start": 3, "body": "Title?", "review_id": review["id"]})
            assert pending.status_code == 201, pending.text
            assert rev.get(url).json()["open_threads"] == 2
        # The pending thread is the reviewer's own until they submit.
        assert client.get(url).json()["open_threads"] == 1
        assert client.get("/api/projects").json()[0]["open_threads"] == 1


def _names(r) -> list[str]:
    assert r.status_code == 200, r.text
    return sorted(p["name"] for p in r.json()["projects"])


def test_a_shared_timeline_shows_the_chart_and_nothing_else():
    from fastapi.testclient import TestClient

    from app.main import app

    with running_app() as client:
        sign_in(client)
        client.patch("/api/auth/me", json={"name": "Didi"})
        p = client.post("/api/projects", json={
            "name": "green", "title": "Green LLMs", "conference": "ICT4S", "year": 2099, "labels": ["secret"],
            "folder": "PhD", "dates": [{"kind": "submission", "date": "2099-03-01"}],
        }).json()
        sub = p["dates"][0]
        assert client.patch(f"/api/projects/{p['id']}/dates/{sub['id']}", json={"date": "2099-03-08"}).status_code == 200
        client.post("/api/projects", json={"name": "gone", "archived": True})
        assert client.get("/api/timeline/share").json() is None

        r = client.post("/api/timeline/share", json={})
        assert r.status_code == 201, r.text
        share = r.json()
        assert share["folder"] is None and share["url"].endswith(f"/timeline/{share['token']}")
        assert client.get("/api/timeline/share").json()["token"] == share["token"]

        with TestClient(app) as anyone:
            r = anyone.get(f"/api/shared/timeline/{share['token']}")
            assert r.status_code == 200, r.text
            assert r.headers["x-robots-tag"] == "noindex"
            t = r.json()
            assert (t["owner"], t["writing_days"]) == ("Didi", 30)
            assert [x["name"] for x in t["projects"]] == ["green"]
            row = t["projects"][0]
            assert (row["title"], row["conference"], row["writing"]["date_id"]) == ("Green LLMs", "ICT4S", sub["id"])
            for hidden in ("labels", "folder_path", "folder_id", "role", "url", "question_count", "open_threads"):
                assert hidden not in row
            moved = row["dates"][0]["changes"][0]
            assert moved["from"] and "by" not in moved
            assert "didi@example.com" not in r.text and "secret" not in r.text

            page = anyone.get(f"/timeline/{share['token']}")
            assert page.status_code == 200 and page.headers["referrer-policy"] == "no-referrer"
            assert "noindex" in page.headers["x-robots-tag"]
            # Everything else still needs a session.
            assert anyone.get("/api/timeline").status_code == 401
            assert anyone.get("/api/timeline/share").status_code == 401
            assert anyone.get(f"/api/projects/{p['id']}").status_code == 401
            assert anyone.get("/api/shared/timeline/not-a-token").status_code == 404


def test_a_shared_timeline_can_show_one_folder_and_be_replaced_or_stopped():
    from fastapi.testclient import TestClient

    from app.main import app

    with running_app() as client:
        sign_in(client)
        client.post("/api/projects", json={"name": "phd-1", "folder": "PhD/2027"})
        client.post("/api/projects", json={"name": "side", "folder": "Side"})
        client.post("/api/projects", json={"name": "loose"})
        share = client.post("/api/timeline/share", json={"folder": "PhD"}).json()
        assert share["folder"] == "PhD"
        url = f"/api/shared/timeline/{share['token']}"

        with TestClient(app) as anyone:
            # The folder and the folders below it.
            assert _names(anyone.get(url)) == ["phd-1"]
            changed = client.patch("/api/timeline/share", json={"folder": None}).json()
            assert (changed["token"], changed["folder"]) == (share["token"], None)
            assert _names(anyone.get(url)) == ["loose", "phd-1", "side"]
            assert client.patch("/api/timeline/share", json={"folder": "Nope"}).status_code == 404

            renewed = client.post("/api/timeline/share", json={}).json()
            assert renewed["token"] != share["token"]
            assert anyone.get(url).status_code == 404
            assert _names(anyone.get(f"/api/shared/timeline/{renewed['token']}")) == ["loose", "phd-1", "side"]

            assert client.delete("/api/timeline/share").status_code == 200
            assert client.get("/api/timeline/share").json() is None
            assert anyone.get(f"/api/shared/timeline/{renewed['token']}").status_code == 404
            assert client.patch("/api/timeline/share", json={"folder": None}).status_code == 404

            # Deleting the shared folder ends the link instead of showing every paper.
            empty = client.post("/api/folders", json={"name": "Empty"}).json()
            last = client.post("/api/timeline/share", json={"folder": empty["id"]}).json()
            assert client.delete(f"/api/folders/{empty['id']}").status_code == 200
            assert client.get("/api/timeline/share").json() is None
            assert anyone.get(f"/api/shared/timeline/{last['token']}").status_code == 404

        # Someone else's folder is not there for you.
        mine = client.post("/api/folders", json={"name": "Mine"}).json()
        with second_client("other@example.com") as other:
            assert other.post("/api/timeline/share", json={"folder": mine["id"]}).status_code == 404
