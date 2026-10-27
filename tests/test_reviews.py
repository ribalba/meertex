"""Reviews and the life of a thread (DESIGN §13): pending visibility, submit and
discard, who may do what, resolve with its word diff, applying a suggestion,
id prefixes, notifications and the list across projects."""

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

REVIEW_KEYS = {"id", "reviewer", "state", "verdict", "summary", "commit", "version", "created_at", "submitted_at",
               "counts"}
REVIEWER = "rita@example.com"
EDITOR = "eddi@example.com"
VIEWER = "vera@example.com"


@pytest.fixture
def mails(monkeypatch) -> list[dict]:
    return capture_mails(monkeypatch)


def _team(client, pid: str) -> None:
    for email, role in ((REVIEWER, "reviewer"), (EDITOR, "editor"), (VIEWER, "viewer")):
        r = client.post(f"/api/projects/{pid}/members", json={"email": email, "role": role})
        assert r.status_code == 201, r.text


def test_a_review_from_start_to_submit(mails):
    from app import live

    with running_app() as client:
        sign_in(client)
        p = project_with(client)
        pid = p["id"]
        _team(client, pid)
        head = snapshot(pid)
        assert client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"}).status_code == 201

        with second_client(REVIEWER) as rita:
            r = rita.post(f"/api/projects/{pid}/reviews", json={"ref": "submission"})
            assert r.status_code == 201, r.text
            review = r.json()
            assert set(review) == REVIEW_KEYS
            assert (review["state"], review["verdict"], review["commit"]) == ("pending", None, head)
            assert review["version"]["tag"] == "submission"
            assert review["reviewer"]["email"] == REVIEWER
            assert review["counts"] == {"open": 0, "resolved": 0, "accepted": 0}
            # A second start returns the pending one.
            again = rita.post(f"/api/projects/{pid}/reviews")
            assert (again.status_code, again.json()["id"]) == (200, review["id"])

            t = thread(rita, pid, "Energie", body="Which energy?", review_id=review["id"][:8])
            assert (t["pending"], t["review_id"]) == (True, review["id"])
            t2 = thread(rita, pid, "The end.", body="Too short.", review_id=review["id"])
            assert [x["id"] for x in rita.get(f"/api/projects/{pid}/threads").json()] == [t["id"], t2["id"]]
            assert rita.get(f"/api/projects/{pid}/reviews").json()[0]["counts"]["open"] == 2

            # Nobody else sees a pending review or its threads.
            for other in (client,):
                assert other.get(f"/api/projects/{pid}/reviews").json() == []
                assert other.get(f"/api/projects/{pid}/threads").json() == []
                assert other.get("/api/threads").json() == []
                assert other.get(f"/api/projects/{pid}/threads/{t['id']}").status_code == 404
                assert other.get(f"/api/projects/{pid}/reviews/{review['id']}").status_code == 404
                assert other.post(f"/api/projects/{pid}/threads/{t['id']}/comments", json={"body": "x"}).status_code == 404
            # Pending threads have no state to change yet.
            assert rita.post(f"/api/projects/{pid}/threads/{t['id']}/reopen").status_code == 422
            assert not mails

            # Threads go into your own pending review only.
            with second_client(EDITOR) as eddi:
                r = eddi.post(f"/api/projects/{pid}/threads", json={
                    "path": "main.tex", "line_start": 1, "body": "x", "review_id": review["id"]})
                assert r.status_code == 404
                assert eddi.post(f"/api/projects/{pid}/reviews/{review['id']}/submit",
                                 json={"verdict": "approve"}).status_code == 404

            r = rita.post(f"/api/projects/{pid}/reviews/{review['id']}/submit",
                          json={"verdict": "changes_requested", "summary": "Good start.\nTwo things."})
            assert r.status_code == 200, r.text
            done = r.json()
            assert (done["state"], done["verdict"], done["summary"]) == ("submitted", "changes_requested",
                                                                       "Good start.\nTwo things.")
            assert done["submitted_at"]
            assert rita.post(f"/api/projects/{pid}/reviews/{review['id']}/submit",
                             json={"verdict": "approve"}).status_code == 422
            assert rita.post(f"/api/projects/{pid}/reviews/{review['id']}/submit",
                             json={"verdict": "great"}).status_code == 422
            assert rita.delete(f"/api/projects/{pid}/reviews/{review['id']}").status_code == 422

        # The owner and the editor hear of it, the viewer and the reviewer do not.
        assert sorted(m["to"] for m in mails) == [OWNER, EDITOR]
        mail = mails[0]
        assert mail["subject"] == f"{REVIEWER} reviewed Green LLMs"
        assert "asked for changes" in mail["body"] and "Two things." in mail["body"]
        assert f"http://testserver/p/paper/reviews/{review['id']}" in mail["body"]
        assert "The review has 2 comments" in mail["body"]

        listed = client.get(f"/api/projects/{pid}/reviews").json()
        assert [x["id"] for x in listed] == [review["id"]]
        got = client.get(f"/api/projects/{pid}/reviews/{review['id'][:8]}").json()
        assert [x["id"] for x in got["threads"]] == [t["id"], t2["id"]]
        assert all(x["pending"] is False for x in got["threads"])
        assert [x["id"] for x in client.get(f"/api/projects/{pid}/threads", params={"review": review["id"]}).json()] == [
            t["id"], t2["id"]]

        # Changes since the review are a word diff from its commit to live.
        put(client, pid, "main.tex", TEXT.replace("The end.", "Das Ende."))
        changes = client.get(f"/api/projects/{pid}/reviews/{review['id']}/changes").json()
        assert changes["from"] == head and changes["to"] == live.resolve_commit(pid, "HEAD")
        [f] = changes["files"]
        assert "-The" in f["diff"] and "+Das" in f["diff"]


def test_discard_and_one_pending_review_each():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        mine = client.post(f"/api/projects/{pid}/reviews", json={"ref": "live"}).json()
        t = thread(client, pid, "Energie", review_id=mine["id"])
        with second_client(REVIEWER) as rita:
            theirs = rita.post(f"/api/projects/{pid}/reviews").json()
            assert theirs["id"] != mine["id"]
            assert rita.delete(f"/api/projects/{pid}/reviews/{mine['id']}").status_code == 404
        assert client.delete(f"/api/projects/{pid}/reviews/{mine['id']}").json() == {"ok": True}
        assert client.get(f"/api/projects/{pid}/threads/{t['id']}").status_code == 404
        assert client.get(f"/api/projects/{pid}/reviews").json() == []
        assert client.post(f"/api/projects/{pid}/reviews", json={"ref": "nothing-here"}).status_code == 404
        assert client.post(f"/api/projects/{pid}/reviews", json={"ref": "--all"}).status_code == 422


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
            assert mails == []
            rita.post(f"/api/projects/{pid}/reviews/{review['id']}/submit", json={"verdict": "comment"})
            mails.clear()
            client.post(f"/api/projects/{pid}/threads/{t2['id']}/comments", json={"body": "Ok."})
            assert f"http://testserver/p/paper/reviews/{review['id']}#thread-{t2['id']}" in mails[0]["body"]


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
            hidden = thread(rita, b["id"], "Köln", review_id=review["id"])
            mine = [t["id"] for t in rita.get("/api/threads").json()]
            assert mine == [tb0["id"], tb1["id"], hidden["id"]]
        listed = client.get("/api/threads").json()
        assert [t["id"] for t in listed] == [ta["id"], tb0["id"], tb1["id"]]
        assert listed[0]["project"] == {"id": a["id"], "name": "alpha", "title": "Alpha"}
        assert "project" not in client.get(f"/api/projects/{a['id']}/threads").json()[0]
        client.post(f"/api/projects/{a['id']}/threads/{ta['id']}/resolve")
        assert [t["id"] for t in client.get("/api/threads").json()] == [tb0["id"], tb1["id"]]
        assert [t["id"] for t in client.get("/api/threads", params={"state": "resolved"}).json()] == [ta["id"]]
        assert len(client.get("/api/threads", params={"state": "all"}).json()) == 3
