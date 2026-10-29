"""Members, roles and invites (DESIGN §2)."""

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db


def _invites(monkeypatch) -> list[dict]:
    from app.routers import members

    sent: list[dict] = []

    def capture(to: str, inviter: str, project_title: str, role: str, link: str) -> None:
        sent.append({"to": to, "inviter": inviter, "title": project_title, "role": role, "link": link})

    monkeypatch.setattr(members, "send_invite", capture)
    return sent


def test_invite_creates_the_account_and_mails_a_link(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    sent = _invites(monkeypatch)
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "green", "title": "Green LLMs"}).json()
        r = client.post(f"/api/projects/{p['id']}/members", json={"email": " Arne@Example.com ", "role": "editor"})
        assert r.status_code == 201, r.text
        member = r.json()
        assert set(member) == {"user_id", "email", "name", "avatar_url", "role", "added_at"}
        assert (member["email"], member["role"]) == ("arne@example.com", "editor")
        [mail] = sent
        assert (mail["to"], mail["inviter"], mail["title"], mail["role"]) == (
            "arne@example.com",
            "didi@example.com",
            "Green LLMs",
            "editor",
        )
        assert mail["link"].startswith("http://testserver/api/auth/callback?token=")

        # The membership is there before Arne ever signs in, and the link lands on the project.
        with TestClient(app) as arne:
            r = arne.get(mail["link"].removeprefix("http://testserver"), follow_redirects=False)
            assert r.status_code == 302
            # By id: Arne may own a project called "green" himself.
            assert r.headers["location"] == f"/p/{p['id']}"
            assert arne.get("/api/projects/green").json()["role"] == "editor"

        members = client.get(f"/api/projects/{p['id']}/members").json()
        assert [(m["email"], m["role"]) for m in members] == [("didi@example.com", "owner"), ("arne@example.com", "editor")]
        assert client.post(f"/api/projects/{p['id']}/members", json={"email": "arne@example.com", "role": "viewer"}).status_code == 409
        assert client.post(f"/api/projects/{p['id']}/members", json={"email": "didi@example.com", "role": "viewer"}).status_code == 409


def test_the_invite_link_does_not_expire(monkeypatch):
    """An invite may sit unread for days. Its link still signs in a year later,
    while a sign-in link the person asked for is gone after 30 minutes."""
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app import security
    from app.main import app
    from app.routers import auth

    sent = _invites(monkeypatch)
    asked = []
    monkeypatch.setattr(auth, "send_login_link", lambda to, link, code: asked.append(link))
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "green", "title": "Green LLMs"}).json()
        r = client.post(f"/api/projects/{p['id']}/members", json={"email": "arne@example.com", "role": "reviewer"})
        assert r.status_code == 201, r.text
        assert client.post("/api/auth/login", json={"email": "arne@example.com"}).status_code == 200
        invite, login = (link.removeprefix("http://testserver") for link in (sent[0]["link"], asked[0]))

        later = security.utcnow() + timedelta(days=365)
        monkeypatch.setattr(security, "utcnow", lambda: later)
        with TestClient(app) as arne:
            assert arne.get(login, follow_redirects=False).status_code == 400
            r = arne.get(invite, follow_redirects=False)
            assert r.status_code == 302
            assert r.headers["location"] == f"/p/{p['id']}"
            assert arne.get("/api/auth/me").json()["email"] == "arne@example.com"


def test_who_may_add_whom(monkeypatch):
    _invites(monkeypatch)
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "green"}).json()
        url = f"/api/projects/{p['id']}/members"
        assert client.post(url, json={"email": "x@example.com", "role": "owner"}).status_code == 422
        assert client.post(url, json={"email": "not an email", "role": "viewer"}).status_code == 422
        client.post(url, json={"email": "ed@example.com", "role": "editor"})
        client.post(url, json={"email": "vic@example.com", "role": "viewer"})
        with second_client("ed@example.com") as ed:
            assert ed.post(url, json={"email": "rev@example.com", "role": "reviewer"}).status_code == 201
            assert ed.post(url, json={"email": "v2@example.com", "role": "viewer"}).status_code == 201
            assert ed.post(url, json={"email": "e2@example.com", "role": "editor"}).status_code == 403
            assert ed.get(url).status_code == 200
        with second_client("vic@example.com") as vic:
            assert vic.post(url, json={"email": "v3@example.com", "role": "viewer"}).status_code == 403
            assert len(vic.get(url).json()) == 5
        with second_client("eve@example.com") as eve:
            assert eve.get(url).status_code == 404
            assert eve.post(url, json={"email": "v3@example.com", "role": "viewer"}).status_code == 404


def test_roles_change_and_members_leave(monkeypatch):
    _invites(monkeypatch)
    with running_app() as client:
        me = sign_in(client)
        p = client.post("/api/projects", json={"name": "green"}).json()
        url = f"/api/projects/{p['id']}/members"
        ed = client.post(url, json={"email": "ed@example.com", "role": "editor"}).json()
        vic = client.post(url, json={"email": "vic@example.com", "role": "viewer"}).json()

        r = client.patch(f"{url}/{vic['user_id']}", json={"role": "reviewer"})
        assert r.status_code == 200
        assert r.json()["role"] == "reviewer"
        assert client.patch(f"{url}/{vic['user_id']}", json={"role": "owner"}).status_code == 422
        assert client.patch(f"{url}/{me['id']}", json={"role": "editor"}).status_code == 422
        assert client.patch(f"{url}/nobody", json={"role": "viewer"}).status_code == 404

        with second_client("ed@example.com") as editor:
            assert editor.patch(f"{url}/{vic['user_id']}", json={"role": "viewer"}).status_code == 403
            assert editor.delete(f"{url}/{vic['user_id']}").status_code == 403
            assert editor.delete(f"{url}/{me['id']}").status_code == 403
        with second_client("vic@example.com") as viewer:
            # Leaving is always allowed.
            assert viewer.delete(f"{url}/{vic['user_id']}").status_code == 200
            assert viewer.get(f"/api/projects/{p['id']}").status_code == 404
        assert client.delete(f"{url}/{me['id']}").status_code == 422
        assert client.delete(f"{url}/{ed['user_id']}").status_code == 200
        assert [m["role"] for m in client.get(url).json()] == ["owner"]


def test_people_are_those_who_share_a_paper(monkeypatch):
    _invites(monkeypatch)
    with running_app() as client:
        sign_in(client)
        green = client.post("/api/projects", json={"name": "green"}).json()
        blue = client.post("/api/projects", json={"name": "blue"}).json()
        client.post(f"/api/projects/{green['id']}/members", json={"email": "vic@example.com", "role": "viewer"})
        client.post(f"/api/projects/{green['id']}/members", json={"email": "ed@example.com", "role": "editor"})
        client.post(f"/api/projects/{blue['id']}/members", json={"email": "ed@example.com", "role": "reviewer"})
        with second_client("eve@example.com") as eve:
            # Eve's coauthor is no one else's business.
            own = eve.post("/api/projects", json={"name": "red"}).json()
            eve.post(f"/api/projects/{own['id']}/members", json={"email": "mal@example.com", "role": "viewer"})
            assert [x["email"] for x in eve.get("/api/people").json()] == ["mal@example.com"]

        people = client.get("/api/people").json()
        # Ed shares two papers with Didi and comes first. Didi is not on the list.
        assert [(x["email"], x["papers"]) for x in people] == [("ed@example.com", 2), ("vic@example.com", 1)]
        assert set(people[0]) == {"user_id", "email", "name", "papers"}
        with second_client("vic@example.com") as vic:
            assert [x["email"] for x in vic.get("/api/people").json()] == ["didi@example.com", "ed@example.com"]
