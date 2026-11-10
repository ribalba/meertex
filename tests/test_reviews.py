"""Reviews and the life of a thread (DESIGN §13): everybody sees a review at
once, one open review per person and commit, no submit step, discard, who may
do what, resolve with its word diff, applying a suggestion, id prefixes,
notifications, the list across projects and the migration from pending."""

import pytest
from helpers import needs_db, running_app, second_client, sign_in
from reviewing import (
    OWNER,
    TEXT,
    capture_mails,
    live_text,
    project_with,
    put,
    snapshot,
    thread,
)

pytestmark = needs_db

REVIEW_KEYS = {"id", "reviewer", "state", "verdict", "summary", "mentions", "commit", "version", "created_at",
               "submitted_at", "counts"}
REVIEWER = "rita@example.com"
EDITOR = "eddi@example.com"
VIEWER = "vera@example.com"
NO_SUBMIT = (
    "Reviews have no submit step any more. Everybody on the paper sees your comments as soon as you write them."
)


@pytest.fixture
def mails(monkeypatch) -> list[dict]:
    return capture_mails(monkeypatch)


def _team(client, pid: str) -> None:
    for email, role in ((REVIEWER, "reviewer"), (EDITOR, "editor"), (VIEWER, "viewer")):
        r = client.post(f"/api/projects/{pid}/members", json={"email": email, "role": role})
        assert r.status_code == 201, r.text


def test_everybody_sees_a_review_and_acts_on_its_threads_at_once(mails):
    from app import live

    with running_app() as client:
        sign_in(client)
        p = project_with(client)
        pid = p["id"]
        _team(client, pid)
        head = snapshot(pid)
        assert client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"}).status_code == 201

        with second_client(REVIEWER) as rita, second_client(EDITOR) as eddi, second_client(VIEWER) as vera:
            r = rita.post(f"/api/projects/{pid}/reviews", json={"ref": "submission"})
            assert r.status_code == 201, r.text
            review = r.json()
            assert set(review) == REVIEW_KEYS
            assert (review["state"], review["verdict"], review["summary"], review["mentions"]) == ("open", None, "", [])
            assert (review["commit"], review["submitted_at"]) == (head, None)
            assert review["version"]["tag"] == "submission"
            assert review["reviewer"]["email"] == REVIEWER
            assert review["counts"] == {"open": 0, "resolved": 0, "accepted": 0}

            t = thread(rita, pid, "Energie", body="Which energy?", review_id=review["id"][:8])
            assert t["review_id"] == review["id"]
            assert "pending" not in t
            t2 = thread(rita, pid, "The end.", body="Too short.", suggestion="Das Ende.", review_id=review["id"])

            # Everybody sees the review and its threads as soon as they are written.
            for other in (client, eddi, vera, rita):
                assert [x["id"] for x in other.get(f"/api/projects/{pid}/reviews").json()] == [review["id"]]
                assert other.get(f"/api/projects/{pid}/reviews").json()[0]["counts"]["open"] == 2
                assert [x["id"] for x in other.get(f"/api/projects/{pid}/threads").json()] == [t["id"], t2["id"]]
                assert [x["id"] for x in other.get("/api/threads").json()] == [t["id"], t2["id"]]
                assert other.get(f"/api/projects/{pid}/threads/{t['id']}").json()["review_id"] == review["id"]
                got = other.get(f"/api/projects/{pid}/reviews/{review['id'][:8]}").json()
                assert [x["id"] for x in got["threads"]] == [t["id"], t2["id"]]
                assert not any("pending" in x for x in got["threads"])
            listed = client.get(f"/api/projects/{pid}/threads", params={"review": review["id"]}).json()
            assert [x["id"] for x in listed] == [t["id"], t2["id"]]

            # And answers them right away: a reply, a resolve, an accept, an apply.
            url = f"/api/projects/{pid}/threads"
            assert client.post(f"{url}/{t['id']}/comments", json={"body": "The GPU's."}).status_code == 201
            assert eddi.post(f"{url}/{t['id']}/resolve", json={"note": "Said so."}).json()["state"] == "resolved"
            assert rita.post(f"{url}/{t['id']}/accept").json()["state"] == "accepted"
            r = eddi.post(f"{url}/{t2['id']}/apply")
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "resolved"
            assert live_text(pid) == TEXT.replace("The end.", "Das Ende.")
            assert rita.post(f"{url}/{t2['id']}/reopen", json={"note": "Shorter still."}).json()["state"] == "open"
            assert client.get(f"/api/projects/{pid}/reviews").json()[0]["counts"] == {
                "open": 1, "resolved": 0, "accepted": 1}

            # Threads go into your own open review only.
            r = eddi.post(f"/api/projects/{pid}/threads", json={
                "path": "main.tex", "line_start": 1, "body": "x", "review_id": review["id"]})
            assert (r.status_code, r.json()["detail"]) == (422, "A thread can only be added to your own open review")

        # Changes since the review are a word diff from its commit to live.
        changes = client.get(f"/api/projects/{pid}/reviews/{review['id']}/changes").json()
        assert changes["from"] == head and changes["to"] == live.resolve_commit(pid, "HEAD")
        [f] = changes["files"]
        assert "-The" in f["diff"] and "+Das" in f["diff"]


def test_one_open_review_per_person_and_commit():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        url = f"/api/projects/{pid}/reviews"
        r = client.post(url, json={"ref": "live"})
        assert r.status_code == 201, r.text
        first = r.json()
        # The same commit, by any name, gives the same review back.
        for ref in ("live", "HEAD", first["commit"][:10]):
            again = client.post(url, json={"ref": ref})
            assert (again.status_code, again.json()["id"]) == (200, first["id"])
        assert client.post(url).json()["id"] == first["id"]

        # Somebody else's review of the same commit is a review of their own.
        with second_client(REVIEWER) as rita:
            r = rita.post(url, json={"ref": "HEAD"})
            assert r.status_code == 201, r.text
            theirs = r.json()
        assert theirs["id"] != first["id"] and theirs["commit"] == first["commit"]

        # The text moves on. A review of the new state is a new one, and the first stays open.
        put(client, pid, "main.tex", TEXT.replace("The end.", "Das Ende."))
        r = client.post(url, json={"ref": "live"})
        assert r.status_code == 201, r.text
        second = r.json()
        assert second["commit"] != first["commit"]
        again = client.post(url, json={"ref": first["commit"]})
        assert (again.status_code, again.json()["id"]) == (200, first["id"])

        listed = client.get(url).json()
        assert [x["id"] for x in listed] == [second["id"], theirs["id"], first["id"]]
        assert {x["state"] for x in listed} == {"open"}
        assert client.post(url, json={"ref": "nothing-here"}).status_code == 404
        assert client.post(url, json={"ref": "--all"}).status_code == 422


def test_there_is_no_submit_step_any_more():
    from fastapi.testclient import TestClient

    from app.main import app

    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        review = client.post(f"/api/projects/{pid}/reviews").json()
        url = f"/api/projects/{pid}/reviews/{review['id']}/submit"
        # Older clients send a verdict and learn why it is gone.
        for body in ({"verdict": "approve", "summary": "Fine."}, None):
            r = client.post(url, json=body)
            assert (r.status_code, r.json()["detail"]) == (410, NO_SUBMIT)
        with second_client(REVIEWER) as rita:
            assert rita.post(url, json={"verdict": "comment"}).status_code == 410
        with TestClient(app) as anyone:
            assert anyone.post(url).status_code == 401
        [still] = client.get(f"/api/projects/{pid}/reviews").json()
        assert (still["state"], still["verdict"], still["submitted_at"]) == ("open", None, None)
        # Nothing is in progress any more, so that list is gone too.
        assert client.get(f"/api/projects/{pid}/reviews-in-progress").status_code == 404


def test_only_an_open_review_without_comments_can_be_discarded():
    import uuid

    from app.database import SessionLocal
    from app.models import Review, utcnow

    with running_app() as client:
        me = sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        url = f"/api/projects/{pid}/reviews"
        mine = client.post(url, json={"ref": "live"}).json()
        t = thread(client, pid, "Energie", review_id=mine["id"])
        with second_client(REVIEWER) as rita:
            r = rita.delete(f"{url}/{mine['id']}")
            assert (r.status_code, r.json()["detail"]) == (403, "Only the reviewer can discard a review")
            # Her own one without comments, started by mistake, goes.
            theirs = rita.post(url).json()
            assert rita.delete(f"{url}/{theirs['id']}").json() == {"ok": True}
            assert rita.get(f"{url}/{theirs['id']}").status_code == 404

        # Everybody has seen its comments, so it stays while it has any.
        r = client.delete(f"{url}/{mine['id']}")
        assert (r.status_code, r.json()["detail"]) == (
            422, "This review has comments, so it cannot be discarded. Delete them first.")
        assert client.get(f"/api/projects/{pid}/threads/{t['id']}").status_code == 200
        gone = client.delete(f"/api/projects/{pid}/comments/{t['comments'][0]['id']}").json()
        assert gone == {"ok": True, "thread": None}
        assert client.delete(f"{url}/{mine['id']}").json() == {"ok": True}
        assert client.get(f"{url}/{mine['id']}").status_code == 404
        assert client.get(url).json() == []

        # A review submitted before the submit step went away keeps its verdict and stays.
        with SessionLocal() as db:
            db.add(Review(id=str(uuid.uuid4()), project_id=pid, reviewer_id=me["id"], commit=mine["commit"],
                          state="submitted", verdict="approve", summary="Fine.", submitted_at=utcnow()))
            db.commit()
        [old] = client.get(url).json()
        assert (old["state"], old["verdict"], old["summary"]) == ("submitted", "approve", "Fine.")
        assert old["submitted_at"]
        r = client.delete(f"{url}/{old['id']}")
        assert (r.status_code, r.json()["detail"]) == (422, "A submitted review cannot be discarded")
        r = client.post(f"/api/projects/{pid}/threads", json={
            "path": "main.tex", "line_start": 1, "body": "x", "review_id": old["id"]})
        assert (r.status_code, r.json()["detail"]) == (422, "A thread can only be added to your own open review")
        # It does not count as an open review of its commit.
        r = client.post(url, json={"ref": old["commit"]})
        assert r.status_code == 201 and r.json()["id"] != old["id"]


def test_the_migration_opens_pending_reviews_and_answers_their_requests():
    from sqlalchemy import text

    from app.database import engine
    from app.migrations import _reviews_pending_open

    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        url = f"/api/projects/{pid}"
        with second_client(REVIEWER) as rita, second_client(EDITOR) as eddi:
            # Rita commented in a pending review, and later in one she submitted.
            pending = rita.post(f"{url}/reviews", json={"ref": "live"}).json()
            thread(rita, pid, "Energie", review_id=pending["id"])
            put(client, pid, "main.tex", TEXT.replace("The end.", "Das Ende."))
            submitted = rita.post(f"{url}/reviews", json={"ref": "live"}).json()
            thread(rita, pid, "Köln", review_id=submitted["id"], text=live_text(pid))
            # Eddi started one and wrote nothing yet.
            empty = eddi.post(f"{url}/reviews").json()
        asked = {}
        for email in (REVIEWER, EDITOR):
            r = client.post(f"{url}/review-requests", json={"reviewer": email})
            assert r.status_code == 201, r.text
            asked[email] = r.json()["id"]

        with engine.begin() as conn:
            conn.execute(text("UPDATE reviews SET state = 'pending' WHERE id IN (:a, :b)"),
                         {"a": pending["id"], "b": empty["id"]})
            conn.execute(text("UPDATE reviews SET state = 'submitted', verdict = 'comment', submitted_at = now()"
                              " WHERE id = :s"), {"s": submitted["id"]})
            assert _reviews_pending_open(conn)
            assert not _reviews_pending_open(conn)

        states = {r["id"]: (r["state"], r["verdict"]) for r in client.get(f"{url}/reviews").json()}
        assert states == {pending["id"]: ("open", None), empty["id"]: ("open", None),
                          submitted["id"]: ("submitted", "comment")}
        # Her open review with comments answers her request. Eddi's has none, so his stays open.
        done = client.get(f"{url}/review-requests/{asked[REVIEWER]}").json()
        assert (done["state"], done["review_id"]) == ("done", pending["id"])
        assert done["done_at"]
        assert client.get(f"{url}/review-requests/{asked[EDITOR]}").json()["state"] == "open"


def test_roles():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        url = f"/api/projects/{pid}"
        own = thread(client, pid, "The end.", suggestion="Das Ende.")

        with second_client(VIEWER) as vera:
            assert vera.post(f"{url}/reviews").status_code == 403
            assert vera.post(f"{url}/threads", json={"path": "main.tex", "line_start": 1, "body": "x"}).status_code == 403
            assert vera.post(f"{url}/threads/{own['id']}/comments", json={"body": "x"}).status_code == 403
            assert vera.get(f"{url}/threads/{own['id']}").status_code == 200

        with second_client(REVIEWER) as rita:
            t = thread(rita, pid, "Energie", body="Which?", suggestion="Leistung")
            reply = rita.post(f"{url}/threads/{own['id']}/comments", json={"body": "Agreed."})
            assert reply.status_code == 201
            for action in ("resolve", "apply"):
                assert rita.post(f"{url}/threads/{t['id']}/{action}").status_code == 403
            # Not her thread but the owner's.
            assert rita.post(f"{url}/threads/{own['id']}/accept").status_code == 403
            # Not her comment.
            assert rita.patch(f"{url}/comments/{own['comments'][0]['id']}", json={"body": "x"}).status_code == 403
            assert rita.delete(f"{url}/comments/{own['comments'][0]['id']}").status_code == 403

            with second_client(EDITOR) as eddi:
                assert eddi.post(f"{url}/threads/{t['id']}/resolve", json={"note": "Fixed."}).status_code == 200
                # An editor who did not start the thread cannot accept it.
                assert eddi.post(f"{url}/threads/{t['id']}/accept").status_code == 403
                assert eddi.post(f"{url}/threads/{t['id']}/reopen").status_code == 200
                assert eddi.post(f"{url}/threads/{t['id']}/apply").status_code == 200
            assert rita.post(f"{url}/threads/{t['id']}/accept").json()["state"] == "accepted"
            assert rita.post(f"{url}/threads/{t['id']}/reopen", json={"note": "Not quite."}).json()["state"] == "open"
        # The owner accepts anything.
        assert client.post(f"{url}/threads/{t['id']}/accept").json()["state"] == "accepted"
        assert live_text(pid) == TEXT.replace("Energie", "Leistung")


def test_resolve_accept_and_reopen_record_events():
    from app import live

    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "Wir messen die Energie 🔋 von LLMs.")
        url = f"/api/projects/{pid}/threads/{t['id']}"
        put(client, pid, "main.tex", TEXT.replace("die Energie", "den Strom"))

        r = client.post(f"{url}/resolve", json={"note": "Strom ist genauer."})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["state"] == "resolved"
        res = got["resolution"]
        assert res["commit"] == live.resolve_commit(pid, "HEAD")
        assert (res["note"], res["by"]["email"]) == ("Strom ist genauer.", OWNER)
        assert res["diff"] == "@@ -2 +2 @@\n Wir messen \n-die Energie\n+den Strom\n  🔋 von LLMs.\n~\n"
        assert [(c["kind"], c["body"]) for c in got["comments"]] == [
            ("comment", "Why?"), ("resolved", "Strom ist genauer.")]
        assert client.post(f"{url}/resolve").status_code == 422
        assert client.post(f"{url}/apply").status_code == 422

        got = client.post(f"{url}/accept").json()
        assert got["state"] == "accepted" and got["accepted"]["by"]["email"] == OWNER
        assert got["resolution"]["note"] == "Strom ist genauer."
        assert client.post(f"{url}/accept").status_code == 422

        got = client.post(f"{url}/reopen", json={"note": "Doch nicht."}).json()
        assert (got["state"], got["resolution"], got["accepted"]) == ("open", None, None)
        assert [c["kind"] for c in got["comments"]] == ["comment", "resolved", "accepted", "reopened"]
        assert client.post(f"{url}/reopen").status_code == 422
        # An unchanged span resolves with an empty diff.
        put(client, pid, "main.tex", TEXT)
        assert client.post(f"{url}/resolve").json()["resolution"]["diff"] == ""

        listed = {s: [x["id"] for x in client.get(f"/api/projects/{pid}/threads", params={"state": s}).json()]
                  for s in ("open", "resolved", "accepted", "all")}
        assert listed == {"open": [], "resolved": [t["id"]], "accepted": [], "all": [t["id"]]}
        assert client.get(f"/api/projects/{pid}/threads", params={"state": "done"}).status_code == 422


def test_an_open_thread_needs_a_suggestion_to_be_accepted():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "Energie")
        url = f"/api/projects/{pid}/threads/{t['id']}"
        r = client.post(f"{url}/accept")
        assert r.status_code == 422 and "no suggestion" in r.json()["detail"]
        # A reply that suggests something is enough, even a deletion.
        assert client.post(f"{url}/comments", json={"body": "Drop it.", "suggestion": ""}).status_code == 201
        got = client.post(f"{url}/accept").json()
        assert (got["state"], got["resolution"]) == ("accepted", None)


def test_apply_a_suggestion_writes_the_live_document():
    from app import live

    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "Grüße aus Köln", body="Shorter?", suggestion="Grüße aus Bonn")
        url = f"/api/projects/{pid}/threads/{t['id']}"
        newer = client.post(f"{url}/comments", json={"body": "Or this.", "suggestion": "Viele Grüße"}).json()
        older_id = t["comments"][0]["id"]
        # Someone edits elsewhere meanwhile, and the span moves.
        put(client, pid, "main.tex", "Neu.\n" + TEXT)

        r = client.post(f"{url}/apply", json={"comment_id": older_id[:8]})
        assert r.status_code == 200, r.text
        got = r.json()
        expected = "Neu.\n" + TEXT.replace("Grüße aus Köln", "Grüße aus Bonn")
        assert live_text(pid) == expected
        # The resolution commit holds the edit, made in the user's name.
        assert got["resolution"]["commit"] == live.resolve_commit(pid, "HEAD")
        assert live.read_file(pid, "main.tex", got["resolution"]["commit"]).data.decode() == expected
        assert live.history(pid, 1)[0]["editors"] == [OWNER]
        assert got["state"] == "resolved"
        assert got["resolution"]["note"] == "Applied the suggestion"
        assert got["resolution"]["diff"] == "@@ -3 +4 @@\n Grüße aus \n-Köln\n+Bonn\n~\n"
        assert got["comments"][-1]["kind"] == "applied"
        assert (got["text"], got["changed"]) == ("Grüße aus Bonn", True)
        assert len(newer["comments"]) == 2

        # The newest suggestion by default, an empty one deletes, a gone span refuses.
        client.post(f"{url}/reopen")
        assert client.post(f"{url}/apply").status_code == 200
        assert "Viele Grüße 😀" in live_text(pid)
        t2 = thread(client, pid, "The end.", suggestion="", text=live_text(pid))
        assert client.post(f"/api/projects/{pid}/threads/{t2['id']}/apply").status_code == 200
        assert "The end." not in live_text(pid)
        t3 = thread(client, pid, "Neu.", body="Remove?", text=live_text(pid))
        assert client.post(f"/api/projects/{pid}/threads/{t3['id']}/apply").status_code == 422
        client.post(f"/api/projects/{pid}/threads/{t3['id']}/comments", json={"body": "", "suggestion": "Alt."})
        put(client, pid, "main.tex", live_text(pid).replace("Neu.\n", ""))
        r = client.post(f"/api/projects/{pid}/threads/{t3['id']}/apply")
        assert r.status_code == 422 and "gone" in r.json()["detail"]


def test_comments_are_edited_and_deleted_by_their_author():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "Energie", body="First.")
        first = t["comments"][0]["id"]
        url = f"/api/projects/{pid}"
        got = client.patch(f"{url}/comments/{first}", json={"body": "First, edited.", "suggestion": "Strom"}).json()
        c = got["comments"][0]
        assert (c["body"], c["suggestion"]) == ("First, edited.", "Strom")
        assert c["edited_at"]
        got = client.patch(f"{url}/comments/{first[:8]}", json={"suggestion": None}).json()
        assert got["comments"][0]["suggestion"] is None
        assert client.patch(f"{url}/comments/{first}", json={"body": ""}).status_code == 422

        second = client.post(f"{url}/threads/{t['id']}/comments", json={"body": "Second."}).json()["comments"][1]["id"]
        resolved = client.post(f"{url}/threads/{t['id']}/resolve").json()
        event = resolved["comments"][-1]["id"]
        assert client.patch(f"{url}/comments/{event}", json={"body": "x"}).status_code == 422
        assert client.delete(f"{url}/comments/{event}").status_code == 422

        r = client.delete(f"{url}/comments/{second}").json()
        assert r["ok"] and [c["kind"] for c in r["thread"]["comments"]] == ["comment", "resolved"]
        # The last comment takes the thread with it.
        assert client.delete(f"{url}/comments/{first}").json() == {"ok": True, "thread": None}
        assert client.get(f"{url}/threads/{t['id']}").status_code == 404


def test_thread_ids_may_be_prefixes():
    from app.database import SessionLocal
    from app.models import Comment, Thread

    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "Energie")
        url = f"/api/projects/{pid}/threads"
        assert client.get(f"{url}/{t['id'][:8]}").json()["id"] == t["id"]
        assert client.get(f"{url}/{t['id'][:8].upper()}").json()["id"] == t["id"]
        assert client.get(f"{url}/{t['id'][:7]}").status_code == 422
        assert client.get(f"{url}/zzzzzzzz").status_code == 404
        assert client.get(f"{url}/00000000").status_code == 404

        # A second thread that shares the first 8 characters makes them ambiguous.
        group = "0000" if t["id"][9:13] != "0000" else "ffff"
        twin = f"{t['id'][:9]}{group}-4000-8000-000000000000"
        with SessionLocal() as db:
            row = db.get(Thread, t["id"])
            db.add(Thread(id=twin, project_id=pid, base_commit=row.base_commit, base_path="main.tex", base_start=0,
                          base_end=1, base_line_start=1, base_line_end=1, quote="\\", state="open"))
            db.flush()
            db.add(Comment(id="c0000000-0000-4000-8000-000000000000", thread_id=twin, kind="comment", body="x"))
            db.commit()
        r = client.get(f"{url}/{t['id'][:8]}")
        assert r.status_code == 409
        assert sorted(r.json()["detail"]["candidates"]) == sorted([t["id"], twin])
        assert client.get(f"{url}/{t['id'][:13]}").json()["id"] == t["id"]
        assert client.get(f"{url}/{twin}").json()["id"] == twin
        assert client.post(f"{url}/{t['id'][:13]}/resolve").json()["state"] == "resolved"


def test_mails_go_to_everybody_in_the_thread_but_the_actor(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        with second_client(REVIEWER) as rita:
            t = thread(rita, pid, "Energie 🔋 von LLMs", body="Which energy?")
            # A single comment goes to the owner and the editors.
            assert sorted(m["to"] for m in mails) == [OWNER, EDITOR]
            m = mails[0]
            assert m["subject"] == f"{REVIEWER} commented on Green LLMs"
            assert "main.tex, line 2" in m["body"] and "> Energie 🔋 von LLMs" in m["body"]
            assert f"http://testserver/p/paper/reviews#thread-{t['id']}" in m["body"]
            mails.clear()

            url = f"/api/projects/{pid}/threads/{t['id']}"
            client.post(f"{url}/comments", json={"body": "The GPU's."})
            assert [m["to"] for m in mails] == [REVIEWER]
            assert "replied" in mails[0]["subject"] and "The GPU's." in mails[0]["body"]
            mails.clear()
            with second_client(EDITOR) as eddi:
                eddi.post(f"{url}/resolve", json={"note": "Said so."})
            assert sorted(m["to"] for m in mails) == [OWNER, REVIEWER]
            assert all("resolved a thread" in m["subject"] for m in mails)
            mails.clear()
            rita.post(f"{url}/accept")
            assert sorted(m["to"] for m in mails) == [OWNER, EDITOR]
            mails.clear()

            # Somebody who left hears nothing more.
            eddi_id = next(m["user_id"] for m in client.get(f"/api/projects/{pid}/members").json()
                           if m["email"] == EDITOR)
            assert client.delete(f"/api/projects/{pid}/members/{eddi_id}").status_code == 200
            rita.post(f"{url}/reopen", json={"note": "Wait."})
            assert [m["to"] for m in mails] == [OWNER]
            assert "reopened" in mails[0]["subject"] and "Wait." in mails[0]["body"]
            mails.clear()

            # A thread in a review links to the review.
            review = rita.post(f"/api/projects/{pid}/reviews").json()
            t2 = thread(rita, pid, "The end.", review_id=review["id"])
            assert [m["to"] for m in mails] == [OWNER]
            mails.clear()
            client.post(f"/api/projects/{pid}/threads/{t2['id']}/comments", json={"body": "Ok."})
            assert [m["to"] for m in mails] == [REVIEWER]
            assert f"http://testserver/p/paper/reviews/{review['id']}#thread-{t2['id']}" in mails[0]["body"]


def test_review_comments_mail_the_authors_once_a_sitting(mails):
    from sqlalchemy import text

    from app.database import engine
    from app.routers.reviews import REVIEW_MAIL_PAUSE

    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        url = f"/api/projects/{pid}"
        with second_client(REVIEWER) as rita:
            review = rita.post(f"{url}/reviews").json()
            assert mails == []
            thread(rita, pid, "Energie 🔋 von LLMs", body="Which energy?", review_id=review["id"])
            # The review's first comment tells the owner and the editors, not the viewer or the reviewer.
            assert sorted(m["to"] for m in mails) == [OWNER, EDITOR]
            m = mails[0]
            assert m["subject"] == f"{REVIEWER} started a review of Green LLMs"
            body = m["body"]
            assert body.startswith(f'{REVIEWER} started a review of "Green LLMs" with this comment.')
            assert "main.tex, line 2" in body and "> Energie 🔋 von LLMs" in body and "Which energy?" in body
            assert "More comments may follow without a mail each." in body
            assert body.endswith(f"http://testserver/p/paper/reviews/{review['id']}")
            mails.clear()

            # The rest of the sitting mails nobody.
            thread(rita, pid, "Köln", body="Which city?", review_id=review["id"])
            thread(rita, pid, "The end.", body="Too short.", review_id=review["id"])
            assert mails == []

            # After a pause the next comment tells them again.
            with engine.begin() as conn:
                conn.execute(text("UPDATE threads SET created_at = created_at - :pause WHERE review_id = :review"),
                             {"pause": REVIEW_MAIL_PAUSE, "review": review["id"]})
            thread(rita, pid, "Einführung", body="A better title?", review_id=review["id"])
            assert sorted(m["to"] for m in mails) == [OWNER, EDITOR]
            m = mails[0]
            assert m["subject"] == f"{REVIEWER} commented in their review of Green LLMs"
            assert m["body"].startswith(f'{REVIEWER} is back at their review of "Green LLMs" with this comment.')
            assert "A better title?" in m["body"]
            mails.clear()
            thread(rita, pid, "Zürich", review_id=review["id"])
            assert mails == []

        # Nobody hears of their own comment.
        with second_client(EDITOR) as eddi:
            theirs = eddi.post(f"{url}/reviews").json()
            thread(eddi, pid, "Energie", review_id=theirs["id"])
        assert [(m["to"], m["subject"]) for m in mails] == [(OWNER, f"{EDITOR} started a review of Green LLMs")]
        mails.clear()
        mine = client.post(f"{url}/reviews").json()
        thread(client, pid, "Energie", review_id=mine["id"])
        assert [m["to"] for m in mails] == [EDITOR]


def test_threads_across_projects():
    with running_app() as client:
        sign_in(client)
        a = project_with(client, name="alpha", title="Alpha")
        b = project_with(client, name="beta", title="Beta")
        ta = thread(client, a["id"], "The end.")
        tb1 = thread(client, b["id"], "Energie")
        tb0 = thread(client, b["id"], "Einführung")
        with second_client(REVIEWER) as rita:
            invite_r = client.post(f"/api/projects/{b['id']}/members", json={"email": REVIEWER, "role": "reviewer"})
            assert invite_r.status_code == 201
            review = rita.post(f"/api/projects/{b['id']}/reviews").json()
            in_review = thread(rita, b["id"], "Köln", review_id=review["id"])
            mine = [t["id"] for t in rita.get("/api/threads").json()]
            assert mine == [tb0["id"], tb1["id"], in_review["id"]]
        # Her review's thread is the owner's to see too.
        listed = client.get("/api/threads").json()
        assert [t["id"] for t in listed] == [ta["id"], tb0["id"], tb1["id"], in_review["id"]]
        assert listed[0]["project"] == {"id": a["id"], "name": "alpha", "title": "Alpha"}
        assert listed[-1]["project"] == {"id": b["id"], "name": "beta", "title": "Beta"}
        assert "project" not in client.get(f"/api/projects/{a['id']}/threads").json()[0]
        client.post(f"/api/projects/{a['id']}/threads/{ta['id']}/resolve")
        assert [t["id"] for t in client.get("/api/threads").json()] == [tb0["id"], tb1["id"], in_review["id"]]
        assert [t["id"] for t in client.get("/api/threads", params={"state": "resolved"}).json()] == [ta["id"]]
        assert len(client.get("/api/threads", params={"state": "all"}).json()) == 4
