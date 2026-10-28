"""Review requests (DESIGN §13, Requests): asking a member for a review, the
mail with its link, starting the review on the state asked for, answering by
submitting, asking again, withdrawing, and who may do what."""

import pytest
from helpers import needs_db, running_app, second_client, sign_in
from reviewing import OWNER, capture_mails, project_with, put, snapshot

pytestmark = needs_db

REQUEST_KEYS = {"id", "reviewer", "requested_by", "state", "note", "commit", "version", "review_id", "created_at",
                "done_at"}
REVIEWER = "rita@example.com"
EDITOR = "eddi@example.com"
VIEWER = "vera@example.com"


@pytest.fixture
def mails(monkeypatch) -> list[dict]:
    return capture_mails(monkeypatch)


def _team(client, pid: str) -> dict[str, str]:
    """The members' user ids by email."""
    for email, role in ((REVIEWER, "reviewer"), (EDITOR, "editor"), (VIEWER, "viewer")):
        r = client.post(f"/api/projects/{pid}/members", json={"email": email, "role": role})
        assert r.status_code == 201, r.text
    return {m["email"]: m["user_id"] for m in client.get(f"/api/projects/{pid}/members").json()}


def test_a_request_mails_a_link_that_starts_the_review(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        version = client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"}).json()
        # The live text moves on after the version.
        put(client, pid, "main.tex", "Something else.\n")

        r = client.post(f"/api/projects/{pid}/review-requests",
                        json={"reviewer": REVIEWER.upper(), "ref": version["tag"], "note": "The intro, please."})
        assert r.status_code == 201, r.text
        rq = r.json()
        assert set(rq) == REQUEST_KEYS
        assert (rq["state"], rq["note"], rq["commit"], rq["review_id"]) == ("open", "The intro, please.",
                                                                            version["commit"], None)
        assert rq["version"]["tag"] == version["tag"]
        assert (rq["reviewer"]["email"], rq["requested_by"]["email"]) == (REVIEWER, OWNER)

        [mail] = mails
        assert mail["to"] == REVIEWER
        assert mail["subject"] == f"{OWNER} asked you to review Green LLMs"
        assert f'review the version "{version["label"]}" of "Green LLMs".' in mail["body"]
        assert "The intro, please." in mail["body"]
        assert f"http://testserver/p/paper/reviews/requests/{rq['id']}" in mail["body"]
        assert client.get(f"/api/projects/{pid}/review-requests").json() == [rq]

        with second_client(REVIEWER) as rita, second_client(EDITOR) as eddi:
            assert rita.get(f"/api/projects/{pid}/review-requests/{rq['id'][:8]}").json() == rq
            # Only the person asked starts it.
            assert eddi.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start").status_code == 403

            r = rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start")
            assert r.status_code == 201, r.text
            review = r.json()
            assert (review["state"], review["commit"], review["version"]["tag"]) == ("pending", version["commit"],
                                                                                      version["tag"])
            again = rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start")
            assert (again.status_code, again.json()["id"]) == (200, review["id"])
            # Starting it does not answer it yet.
            assert [x["id"] for x in client.get(f"/api/projects/{pid}/review-requests").json()] == [rq["id"]]

            r = rita.post(f"/api/projects/{pid}/reviews/{review['id']}/submit", json={"verdict": "approve"})
            assert r.status_code == 200, r.text

            assert client.get(f"/api/projects/{pid}/review-requests").json() == []
            done = client.get(f"/api/projects/{pid}/review-requests/{rq['id']}").json()
            assert (done["state"], done["review_id"]) == ("done", review["id"])
            assert done["done_at"]
            assert rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start").status_code == 422
            assert client.delete(f"/api/projects/{pid}/review-requests/{rq['id']}").status_code == 422


def test_who_may_ask_and_who_may_be_asked(mails):
    with running_app() as client:
        me = sign_in(client)
        pid = project_with(client)["id"]
        ids = _team(client, pid)
        url = f"/api/projects/{pid}/review-requests"

        assert client.post(url, json={"reviewer": me["id"]}).status_code == 422
        r = client.post(url, json={"reviewer": VIEWER})
        assert r.status_code == 422 and "viewer" in r.json()["detail"]
        assert client.post(url, json={"reviewer": "nobody@example.com"}).status_code == 404
        assert client.post(url, json={"reviewer": REVIEWER, "ref": "nothing-here"}).status_code == 404
        assert not mails

        with second_client(REVIEWER) as rita, second_client(VIEWER) as vera:
            # Reviewers review. Asking is for the authors.
            assert rita.post(url, json={"reviewer": EDITOR}).status_code == 403

            # By user id, on HEAD when no ref is given.
            head = snapshot(pid)
            rq = client.post(url, json={"reviewer": ids[EDITOR]}).json()
            assert (rq["reviewer"]["email"], rq["commit"], rq["version"]) == (EDITOR, head, None)
            assert '"Green LLMs" as it reads now.' in mails[-1]["body"]

            # Asking again replaces the open request and mails again.
            r = client.post(url, json={"reviewer": EDITOR, "ref": "live", "note": "Again, please."})
            assert r.status_code == 200, r.text
            assert (r.json()["id"], r.json()["note"]) == (rq["id"], "Again, please.")
            assert [m["to"] for m in mails] == [EDITOR, EDITOR]
            assert [x["id"] for x in vera.get(url).json()] == [rq["id"]]

            # Editors and the owner withdraw.
            other = client.post(url, json={"reviewer": REVIEWER}).json()
            assert rita.delete(f"{url}/{other['id']}").status_code == 403
            with second_client(EDITOR) as eddi:
                assert eddi.delete(f"{url}/{other['id']}").json() == {"ok": True}
            assert rita.get(f"{url}/{other['id']}").status_code == 404
            assert rita.post(f"{url}/{other['id']}/start").status_code == 404

            # Somebody who cannot review any more drops out of the list.
            assert client.patch(f"/api/projects/{pid}/members/{ids[EDITOR]}", json={"role": "viewer"}).status_code == 200
            assert client.get(url).json() == []


def test_any_review_answers_the_request(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        with second_client(REVIEWER) as rita:
            # A review that was started before the request is the one the request opens.
            mine = rita.post(f"/api/projects/{pid}/reviews", json={"ref": "live"}).json()
            put(client, pid, "main.tex", "Newer text.\n")
            rq = client.post(f"/api/projects/{pid}/review-requests", json={"reviewer": REVIEWER, "ref": "live"}).json()
            assert rq["commit"] != mine["commit"]
            r = rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start")
            assert (r.status_code, r.json()["id"], r.json()["commit"]) == (200, mine["id"], mine["commit"])

            rita.post(f"/api/projects/{pid}/reviews/{mine['id']}/submit", json={"verdict": "comment"})
            done = client.get(f"/api/projects/{pid}/review-requests/{rq['id']}").json()
            assert (done["state"], done["review_id"]) == ("done", mine["id"])
