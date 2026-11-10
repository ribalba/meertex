"""Review requests (DESIGN §13, Requests): asking a member for a review, the
mail with its link, starting the review on the state asked for, answering with
the first comment in a review, asking again, withdrawing, and who may do what."""

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
        assert "Everybody on the paper sees your comments as soon as you write them." in mail["body"]
        assert "pending" not in mail["body"]
        assert f"http://testserver/p/paper/reviews/requests/{rq['id']}" in mail["body"]
        assert client.get(f"/api/projects/{pid}/review-requests").json() == [rq]

        with second_client(REVIEWER) as rita, second_client(EDITOR) as eddi:
            assert rita.get(f"/api/projects/{pid}/review-requests/{rq['id'][:8]}").json() == rq
            # Only the person asked starts it.
            assert eddi.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start").status_code == 403

            r = rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start")
            assert r.status_code == 201, r.text
            review = r.json()
            assert (review["state"], review["commit"], review["version"]["tag"]) == ("open", version["commit"],
                                                                                      version["tag"])
            again = rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start")
            assert (again.status_code, again.json()["id"]) == (200, review["id"])
            # Starting it does not answer it yet.
            assert [x["id"] for x in client.get(f"/api/projects/{pid}/review-requests").json()] == [rq["id"]]

            # Her first comment in the review does.
            r = rita.post(f"/api/projects/{pid}/threads", json={
                "path": "main.tex", "ref": review["commit"], "line_start": 2, "body": "Which energy?",
                "review_id": review["id"]})
            assert r.status_code == 201, r.text
            comment = r.json()["comments"][0]["id"]

            assert client.get(f"/api/projects/{pid}/review-requests").json() == []
            done = client.get(f"/api/projects/{pid}/review-requests/{rq['id']}").json()
            assert (done["state"], done["review_id"]) == ("done", review["id"])
            assert done["done_at"]
            assert rita.post(f"/api/projects/{pid}/review-requests/{rq['id']}/start").status_code == 422
            assert client.delete(f"/api/projects/{pid}/review-requests/{rq['id']}").status_code == 422

            # It stays answered when she deletes the comment and discards the review.
            assert rita.delete(f"/api/projects/{pid}/comments/{comment}").status_code == 200
            assert rita.delete(f"/api/projects/{pid}/reviews/{review['id']}").json() == {"ok": True}
            assert client.get(f"/api/projects/{pid}/review-requests/{rq['id']}").json()["state"] == "done"


def test_without_a_ref_the_snapshot_is_taken_when_the_review_starts(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        asked_at = snapshot(pid)
        url = f"/api/projects/{pid}/review-requests"

        r = client.post(url, json={"reviewer": REVIEWER, "note": "Once more, please."})
        assert r.status_code == 201, r.text
        rq = r.json()
        assert (rq["commit"], rq["version"]) == (None, None)
        assert client.get(url).json() == [rq]
        [mail] = mails
        assert 'asked you to review "Green LLMs".' in mail["body"]
        assert "The link starts your review on the text as it reads when you open it." in mail["body"]

        # The authors fix things after asking. The review starts on the fix.
        put(client, pid, "main.tex", "The fixed text.\n")
        with second_client(REVIEWER) as rita:
            r = rita.post(f"{url}/{rq['id']}/start")
            assert r.status_code == 201, r.text
            review = r.json()
            assert review["commit"] != asked_at
            assert (review["version"], review["state"]) == (None, "open")
            text = client.get(f"/api/projects/{pid}/files/main.tex", params={"ref": review["commit"]}).text
            assert text == "The fixed text.\n"
            # The request keeps no commit, so starting again after a discard
            # takes the text of that moment.
            assert client.get(f"{url}/{rq['id']}").json()["commit"] is None
            assert rita.delete(f"/api/projects/{pid}/reviews/{review['id']}").status_code == 200

            put(client, pid, "main.tex", "Fixed again.\n")
            version = client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"}).json()
            again = rita.post(f"{url}/{rq['id']}/start").json()
            assert (again["commit"], again["version"]["tag"]) == (version["commit"], version["tag"])

            # Asking again with a ref fixes the state at once.
            put(client, pid, "main.tex", "Fixed once more.\n")
            fixed = client.post(url, json={"reviewer": REVIEWER, "ref": "live"}).json()
            assert fixed["id"] == rq["id"]
            assert fixed["commit"] not in (None, version["commit"])
            assert '"Green LLMs" as it reads now.' in mails[-1]["body"]


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

            # By user id, on HEAD.
            head = snapshot(pid)
            rq = client.post(url, json={"reviewer": ids[EDITOR], "ref": "HEAD"}).json()
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


def test_the_first_comment_in_any_of_her_reviews_answers_the_request(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        url = f"/api/projects/{pid}"
        with second_client(REVIEWER) as rita, second_client(EDITOR) as eddi:
            # Her review of the state asked for, started before the request, is the one it opens.
            mine = rita.post(f"{url}/reviews", json={"ref": "live"}).json()
            rq = client.post(f"{url}/review-requests", json={"reviewer": REVIEWER, "ref": "live"}).json()
            assert rq["commit"] == mine["commit"]
            r = rita.post(f"{url}/review-requests/{rq['id']}/start")
            assert (r.status_code, r.json()["id"]) == (200, mine["id"])

            # Asked again for a newer state, starting gives a review of its own.
            put(client, pid, "main.tex", "Newer text.\n")
            r = client.post(f"{url}/review-requests", json={"reviewer": REVIEWER, "ref": "live"})
            assert (r.status_code, r.json()["id"]) == (200, rq["id"])
            rq = r.json()
            assert rq["commit"] != mine["commit"]
            r = rita.post(f"{url}/review-requests/{rq['id']}/start")
            assert r.status_code == 201, r.text
            newer = r.json()
            assert (newer["commit"], newer["state"]) == (rq["commit"], "open")
            assert newer["id"] != mine["id"]

            # Neither starting, nor a single comment, nor somebody else's review comment answers it.
            single = {"path": "main.tex", "line_start": 1, "body": "Just a note."}
            assert rita.post(f"{url}/threads", json=single).status_code == 201
            theirs = eddi.post(f"{url}/reviews").json()
            assert eddi.post(f"{url}/threads", json={**single, "review_id": theirs["id"]}).status_code == 201
            assert client.get(f"{url}/review-requests/{rq['id']}").json()["state"] == "open"

            # Her first comment in any of her reviews does, here the older one.
            r = rita.post(f"{url}/threads", json={
                "path": "main.tex", "ref": mine["commit"], "line_start": 1, "body": "Still true?",
                "review_id": mine["id"]})
            assert r.status_code == 201, r.text
            done = client.get(f"{url}/review-requests/{rq['id']}").json()
            assert (done["state"], done["review_id"]) == ("done", mine["id"])
            assert done["done_at"]
