"""Questions: when they are asked, their text, and what the answers do (DESIGN §6)."""

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db


def _project(client, name: str, dates: list[dict], **body) -> dict:
    r = client.post("/api/projects", json={"name": name, "dates": dates, **body})
    assert r.status_code == 201, r.text
    return r.json()


def _date(p: dict, kind: str) -> dict:
    return next(d for d in p["dates"] if d["kind"] == kind)


def _answer(client, p: dict, kind: str, answer: str, **body):
    return client.post(f"/api/projects/{p['id']}/dates/{_date(p, kind)['id']}/answer", json={"answer": answer, **body})


def test_texts_and_answers_per_kind():
    with running_app() as client:
        sign_in(client)
        p = _project(
            client,
            "all",
            [
                {"kind": "abstract", "date": "2026-01-08"},
                {"kind": "submission", "date": "2026-01-15"},
                {"kind": "supplementary", "due_at": "2026-01-20T14:00", "tz": "UTC+2"},
                {"kind": "rebuttal_start", "date": "2026-02-01"},
                {"kind": "other", "label": "Artifact evaluation", "date": "2026-02-10", "tz": "UTC"},
                {"kind": "conference_start", "date": "2026-03-01"},
            ],
        )
        texts = {q["kind"]: (q["text"], q["answers"]) for q in p["questions"]}
        assert texts == {
            "abstract": ("The abstract registration deadline passed on 8 Jan 2026. Did you register the abstract?", ["yes", "no", "moved"]),
            "submission": ("The paper submission deadline passed on 15 Jan 2026. Did you hand it in?", ["yes", "no", "moved"]),
            "supplementary": (
                (
                    "The supplementary material deadline passed on 20 Jan 2026 at 14:00 UTC+2. "
                    "Did you hand in the supplementary material?"
                ),
                ["yes", "no", "moved"],
            ),
            "other": ('The date "Artifact evaluation" passed on 10 Feb 2026. Is it done?', ["yes", "no", "moved"]),
        }
        q = p["questions"][0]
        assert set(q) == {"project_id", "project_name", "date_id", "kind", "label", "due_at", "text", "answers"}
        assert q["project_name"] == "all"
        assert p["question_count"] == 4
        # Kinds without a question cannot be answered.
        assert _answer(client, p, "rebuttal_start", "yes").status_code == 422
        assert _answer(client, p, "submission", "accepted").status_code == 422
        assert _answer(client, p, "other", "yes").status_code == 200
        assert _answer(client, p, "supplementary", "no").json()["outcome"] is None


def test_review_questions_wait_for_handing_in():
    with running_app() as client:
        sign_in(client)
        p = _project(
            client,
            "review",
            [
                {"kind": "submission", "date": "2026-01-15"},
                {"kind": "rebuttal_end", "date": "2026-02-15"},
                {"kind": "notification", "date": "2026-03-01"},
                {"kind": "camera_ready", "date": "2026-04-01"},
            ],
        )
        assert [q["kind"] for q in p["questions"]] == ["submission"]
        p = _answer(client, p, "submission", "yes").json()
        assert [q["kind"] for q in p["questions"]] == ["rebuttal_end", "notification"]
        assert [v["tag"] for v in p["versions"]] == ["submission"]
        # Answering yes again does not make a second version.
        p = _answer(client, p, "submission", "yes").json()
        assert len(p["versions"]) == 1

        p = _answer(client, p, "rebuttal_end", "yes", version={"at": "now", "note": "3 pages"}).json()
        assert [(v["kind"], v["tag"], v["note"]) for v in p["versions"]][1] == ("rebuttal", "rebuttal", "3 pages")
        p = _answer(client, p, "notification", "accepted").json()
        assert p["outcome"] == "accepted"
        assert [q["kind"] for q in p["questions"]] == ["camera_ready"]
        assert p["questions"][0]["answers"] == ["yes", "moved"]
        p = _answer(client, p, "camera_ready", "yes").json()
        assert [v["tag"] for v in p["versions"]] == ["submission", "rebuttal", "camera-ready"]
        assert p["questions"] == []

        # Without a submission date there is nothing to wait for.
        q = _project(client, "no-submission", [{"kind": "notification", "date": "2026-03-01"}])
        assert [x["kind"] for x in q["questions"]] == ["notification"]


def test_revision_cycle():
    with running_app() as client:
        sign_in(client)
        p = _project(
            client,
            "journal",
            [
                {"kind": "submission", "date": "2026-01-15"},
                {"kind": "notification", "date": "2026-03-01"},
                {"kind": "revision", "date": "2026-05-01"},
            ],
        )
        p = _answer(client, p, "submission", "yes").json()
        p = _answer(client, p, "notification", "revision").json()
        assert p["outcome"] == "revision"
        assert [q["kind"] for q in p["questions"]] == ["revision"]
        p = _answer(client, p, "revision", "yes").json()
        assert p["outcome"] is None
        assert p["outcome_at"] is None
        revision = p["versions"][-1]
        assert (revision["kind"], revision["label"], revision["tag"], revision["number"]) == (
            "revision",
            "Revision 1",
            "revision-1",
            2,
        )
        # The notification was answered. The client asks for the next one.
        assert p["questions"] == []


def test_no_closes_the_paper():
    with running_app() as client:
        sign_in(client)
        p = _project(
            client,
            "skipped",
            [{"kind": "abstract", "date": "2026-01-08"}, {"kind": "submission", "date": "2026-01-15"}],
        )
        p = _answer(client, p, "submission", "no").json()
        assert p["outcome"] == "not_submitted"
        assert p["questions"] == []
        assert p["question_count"] == 0
        # A mistake, corrected.
        p = _answer(client, p, "submission", "yes").json()
        assert p["outcome"] is None
        q = _project(client, "abstract-only", [{"kind": "abstract", "date": "2026-01-08"}])
        assert _answer(client, q, "abstract", "no").json()["outcome"] == "not_submitted"
        r = client.patch(f"/api/projects/{q['id']}", json={"outcome": None}).json()
        assert r["outcome"] is None


def test_questions_across_projects():
    with running_app() as client:
        sign_in(client)
        a = _project(client, "a", [{"kind": "submission", "date": "2026-01-15"}])
        _project(client, "b", [{"kind": "abstract", "date": "2026-01-01"}])
        _project(client, "c", [{"kind": "submission", "date": "2099-01-15"}])
        archived = _project(client, "d", [{"kind": "submission", "date": "2026-01-01"}])
        client.patch(f"/api/projects/{archived['id']}", json={"archived": True})
        closed = _project(client, "e", [{"kind": "submission", "date": "2026-01-01"}])
        client.patch(f"/api/projects/{closed['id']}", json={"outcome": "withdrawn"})

        qs = client.get("/api/questions").json()
        assert [(q["project_name"], q["kind"]) for q in qs] == [("b", "abstract"), ("a", "submission")]

        client.post(f"/api/projects/{a['id']}/members", json={"email": "vic@example.com", "role": "viewer"})
        client.post(f"/api/projects/{a['id']}/members", json={"email": "ed@example.com", "role": "editor"})
        with second_client("vic@example.com") as vic:
            assert vic.get("/api/questions").json() == []
            # The project still shows its question to a viewer.
            assert vic.get("/api/projects/a").json()["question_count"] == 1
        with second_client("ed@example.com") as ed:
            assert [q["project_name"] for q in ed.get("/api/questions").json()] == ["a"]
