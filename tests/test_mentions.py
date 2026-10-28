"""@-mentions in comments, notes and review summaries (DESIGN §13, Mentions):
what counts as one, what is stored and who is mailed."""

import pytest
from helpers import needs_db, running_app, second_client, sign_in
from reviewing import OWNER, capture_mails, project_with, thread

from app.mentions import find
from app.models import User

REVIEWER = "rita@example.com"
EDITOR = "eddi@example.com"
VIEWER = "vera@example.com"


# --- Without a database: what counts as a mention ----------------------------------


def _people(*rows: tuple[str, str, str | None]) -> list[User]:
    return [User(id=i, email=email, name=name) for i, email, name in rows]


PEOPLE = _people(
    ("anna", "anna@example.com", "Anna"),
    ("al", "al@example.com", "Anna Lena"),
    ("jg", "jg@example.com", "Jürgen Groß"),
    ("no", "nobody@example.com", None),
)


def ids(text: str, people: list[User] = PEOPLE) -> list[str]:
    return [u.id for u in find(text, people)]


def test_a_mention_is_an_at_and_a_name_or_an_email():
    assert ids("@Anna Lena, see §3. And @anna too.") == ["al", "anna"]
    assert ids("@jürgen groß: please look") == ["jg"]
    assert ids("(@nobody@example.com)") == ["no"]
    assert ids("Thanks, @Anna. @Anna again.") == ["anna"]


def test_the_longest_name_that_fits_wins():
    assert ids("@Anna Lena") == ["al"]
    # "Anna Lena" would run on into a word, so it is Anna.
    assert ids("@Anna Lenas idea") == ["anna"]
    assert ids("@Annabel") == []


def test_no_mention_inside_words_emails_or_code():
    assert ids("mail anna@example.com or nobody@example.com") == []
    assert ids("`@Anna` and\n```\n@Anna Lena\n```\nend") == []
    assert ids("@stranger and @") == []


def test_one_name_mentions_everybody_who_has_it():
    twins = _people(("k1", "kim1@example.com", "Kim"), ("k2", "kim2@example.com", "Kim"))
    assert ids("@Kim", twins) == ["k1", "k2"]
    assert ids("@kim2@example.com", twins) == ["k2"]


# --- Through the API ---------------------------------------------------------------


@pytest.fixture
def mails(monkeypatch) -> list[dict]:
    return capture_mails(monkeypatch)


def _named(client, name: str) -> None:
    r = client.patch("/api/auth/me", json={"name": name})
    assert r.status_code == 200, r.text


def _team(client, pid: str) -> None:
    _named(client, "Didi Hoffmann")
    for email, role, name in ((REVIEWER, "reviewer", "Rita Reviewer"), (EDITOR, "editor", "Eddi"),
                              (VIEWER, "viewer", "Vera Viewer")):
        r = client.post(f"/api/projects/{pid}/members", json={"email": email, "role": role})
        assert r.status_code == 201, r.text
        with second_client(email) as other:
            _named(other, name)


def _to(mails: list[dict]) -> dict[str, str]:
    """Each recipient's subject. Nobody may get two mails for one action."""
    got = [m["to"] for m in mails]
    assert len(got) == len(set(got)), got
    return {m["to"]: m["subject"] for m in mails}


@needs_db
def test_a_comment_mails_whom_it_mentions_in_place_of_the_usual_mail(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        with second_client(REVIEWER) as rita:
            t = thread(rita, pid, "Energie 🔋 von LLMs", body="@Vera Viewer and @didi hoffmann, which energy? @Rita Reviewer")
        assert [p["email"] for p in t["comments"][0]["mentions"]] == [VIEWER, OWNER, REVIEWER]
        assert _to(mails) == {
            OWNER: "Rita Reviewer mentioned you on Green LLMs",
            VIEWER: "Rita Reviewer mentioned you on Green LLMs",
            EDITOR: "Rita Reviewer commented on Green LLMs",
        }
        vera = next(m for m in mails if m["to"] == VIEWER)["body"]
        assert vera.startswith('Rita Reviewer mentioned you in a comment on "Green LLMs".')
        assert "main.tex, line 2" in vera and "> Energie 🔋 von LLMs" in vera
        assert f"http://testserver/p/paper/reviews#thread-{t['id']}" in vera
        assert "Turn these mails off at http://testserver/settings#mentions." in vera
        mails.clear()

        # A reply by email address. The person who writes is never mailed.
        r = client.post(f"/api/projects/{pid}/threads/{t['id']}/comments",
                        json={"body": "@eddi@example.com, can you? @Didi Hoffmann"})
        assert [p["email"] for p in r.json()["comments"][1]["mentions"]] == [EDITOR, OWNER]
        assert _to(mails) == {
            EDITOR: "Didi Hoffmann mentioned you on Green LLMs",
            REVIEWER: "Didi Hoffmann replied on Green LLMs",
        }


@needs_db
def test_mention_mails_can_be_turned_off(mails):
    with running_app() as client:
        assert sign_in(client)["mention_mails"] is True
        pid = project_with(client)["id"]
        _team(client, pid)
        r = client.patch("/api/auth/me", json={"mention_mails": False})
        assert r.status_code == 200 and r.json()["mention_mails"] is False
        with second_client(VIEWER) as vera:
            assert vera.patch("/api/auth/me", json={"mention_mails": False}).json()["mention_mails"] is False
        with second_client(REVIEWER) as rita:
            t = thread(rita, pid, "The end.", body="@Vera Viewer @Didi Hoffmann")
        # Still mentioned, but the owner gets the usual mail and the viewer none.
        assert len(t["comments"][0]["mentions"]) == 2
        assert _to(mails) == {OWNER: "Rita Reviewer commented on Green LLMs", EDITOR: "Rita Reviewer commented on Green LLMs"}


@needs_db
def test_an_edit_mails_only_the_people_it_adds(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        with second_client(REVIEWER) as rita:
            t = thread(rita, pid, "The end.", body="@Vera Viewer, look.")
            cid = t["comments"][0]["id"]
            mails.clear()
            r = rita.patch(f"/api/projects/{pid}/comments/{cid}", json={"body": "@Vera Viewer and @Eddi, look."})
            assert r.status_code == 200, r.text
            assert [p["email"] for p in r.json()["comments"][0]["mentions"]] == [VIEWER, EDITOR]
            assert _to(mails) == {EDITOR: "Rita Reviewer mentioned you on Green LLMs"}
            assert "mentioned you in a comment" in mails[0]["body"]
            mails.clear()
            rita.patch(f"/api/projects/{pid}/comments/{cid}", json={"body": "@Eddi, look."})
            assert mails == []
            assert rita.get(f"/api/projects/{pid}/threads/{t['id']}").json()["comments"][0]["mentions"][0]["email"] == EDITOR


@needs_db
def test_a_note_mentions_too(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        with second_client(REVIEWER) as rita:
            t = thread(rita, pid, "The end.", body="Shorter?")
        mails.clear()
        with second_client(EDITOR) as eddi:
            r = eddi.post(f"/api/projects/{pid}/threads/{t['id']}/resolve", json={"note": "@Vera Viewer asked for it."})
        event = r.json()["comments"][-1]
        assert (event["kind"], [p["email"] for p in event["mentions"]]) == ("resolved", [VIEWER])
        # The owner did not write in the thread, so only Rita hears of it as before.
        assert _to(mails) == {
            VIEWER: "Eddi mentioned you on Green LLMs",
            REVIEWER: "Eddi resolved a thread on Green LLMs",
        }
        vera = next(m for m in mails if m["to"] == VIEWER)["body"]
        assert vera.startswith('Eddi resolved a thread on "Green LLMs" and mentioned you.')


@needs_db
def test_a_pending_review_tells_its_mentions_when_it_is_submitted(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        mails.clear()
        with second_client(REVIEWER) as rita:
            review = rita.post(f"/api/projects/{pid}/reviews").json()
            t = thread(rita, pid, "The end.", body="@Vera Viewer knows this.", review_id=review["id"])
            rita.post(f"/api/projects/{pid}/threads/{t['id']}/comments", json={"body": "And @Rita Reviewer."})
            assert t["pending"] and mails == []
            r = rita.post(f"/api/projects/{pid}/reviews/{review['id']}/submit",
                          json={"verdict": "changes_requested", "summary": "Thanks, @Eddi."})
            assert r.status_code == 200, r.text
            assert [p["email"] for p in r.json()["mentions"]] == [EDITOR]
        assert _to(mails) == {
            OWNER: "Rita Reviewer reviewed Green LLMs",
            EDITOR: "Rita Reviewer reviewed Green LLMs",
            VIEWER: "Rita Reviewer mentioned you in a review of Green LLMs",
        }
        body = {m["to"]: m["body"] for m in mails}
        assert "It mentions you." in body[EDITOR] and "It mentions you." not in body[OWNER]
        assert body[VIEWER].startswith('Rita Reviewer reviewed "Green LLMs", asked for changes and mentioned you.')
        assert f"http://testserver/p/paper/reviews/{review['id']}" in body[VIEWER]
        assert "settings#mentions" in body[VIEWER]


@needs_db
def test_only_members_can_be_mentioned(mails):
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        _team(client, pid)
        vera_id = next(m["user_id"] for m in client.get(f"/api/projects/{pid}/members").json() if m["email"] == VIEWER)
        assert client.delete(f"/api/projects/{pid}/members/{vera_id}").status_code == 200
        mails.clear()
        t = thread(client, pid, "The end.", body="@Vera Viewer and @nobody@example.com")
        assert t["comments"][0]["mentions"] == []
        assert _to(mails) == {EDITOR: "Didi Hoffmann commented on Green LLMs"}
