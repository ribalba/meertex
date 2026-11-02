"""Sign-in and the account: meerpad's flow, plus the naming pattern."""

from helpers import api_token, needs_db, running_app, sign_in

pytestmark = needs_db


def test_sign_in_with_code_and_token():
    with running_app() as client:
        assert client.get("/api/auth/me").status_code == 401
        me = sign_in(client)
        assert me["email"] == "didi@example.com"
        assert me["naming_pattern"] == "{year}-{conference|lower}-{paper|lower}"
        token = api_token(client)
        client.cookies.clear()
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["email"] == "didi@example.com"
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_naming_pattern_needs_a_placeholder():
    with running_app() as client:
        sign_in(client)
        r = client.patch("/api/auth/me", json={"naming_pattern": "paper"})
        assert r.status_code == 422
        r = client.patch("/api/auth/me", json={"naming_pattern": "{conference}{year}-{paper}"})
        assert r.status_code == 200
        assert r.json()["naming_pattern"] == "{conference}{year}-{paper}"


def test_a_new_account_waits_for_the_browsers_zone():
    """No zone until the web app sends the browser's (app/static/js/app.js)."""
    with running_app() as client:
        assert sign_in(client)["timezone"] is None
        assert client.patch("/api/auth/me", json={"timezone": "Mars/Olympus"}).status_code == 422
        r = client.patch("/api/auth/me", json={"timezone": "Asia/Tokyo"})
        assert r.status_code == 200
        assert r.json()["timezone"] == "Asia/Tokyo"
        # A null is no way back to "not chosen yet".
        r = client.patch("/api/auth/me", json={"timezone": None, "name": "Didi"})
        assert r.status_code == 200
        assert r.json()["timezone"] == "Asia/Tokyo"


def test_the_desktop_app_gets_a_deep_link(monkeypatch):
    """The desktop app (electron/) gets a meertex:// link. It loads the ordinary
    callback with that token inside its window, which signs it in there."""
    from app.config import get_settings
    from app.routers import auth

    links = []
    monkeypatch.setattr(auth, "send_login_link", lambda to, link, code: links.append(link))
    with running_app() as client:
        for body in ({"client": "desktop", "next": "/settings"}, {}, {"client": "cli"}):
            r = client.post("/api/auth/login", json={"email": "didi@example.com", **body})
            assert r.status_code == 200, r.text
        assert client.post("/api/auth/login", json={"email": "didi@example.com", "client": "phone"}).status_code == 422
        desktop, web, cli = links
        assert desktop.startswith("meertex://login?token=")
        callback = f"{get_settings().base_url}/api/auth/callback?token="
        assert web.startswith(callback)
        assert cli.startswith(callback)
        token = desktop.removeprefix("meertex://login?token=")
        r = client.get(f"/api/auth/callback?token={token}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/settings"
        assert client.get("/api/auth/me").json()["email"] == "didi@example.com"


def _new_user_mails(monkeypatch, to: str = "admin@example.com") -> list[tuple[str, str, str]]:
    """Turn the new-account mail on and catch every mail the app sends."""
    from app import emailer
    from app.routers import auth

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(auth.settings, "new_user_mail_to", to)
    monkeypatch.setattr(emailer, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def _to(sent: list, address: str = "admin@example.com") -> list[tuple[str, str]]:
    return [(subject, body) for to, subject, body in sent if to == address]


def test_a_new_account_mails_once_at_its_first_sign_in(monkeypatch):
    """Signing up on your own: one mail at the first sign-in, none after."""
    from app.database import SessionLocal
    from app.security import create_login_code

    sent = _new_user_mails(monkeypatch)
    with running_app() as client:
        sign_in(client)
        [(subject, body)] = _to(sent)
        assert subject == "New user didi@example.com"
        assert "didi@example.com signed in to meertex for the first time." in body
        assert "Nobody invited them. They signed up on their own." in body
        assert "They signed in with the sign-in code in a browser.\n" in body
        assert "Their IP address is testclient and their user agent is testclient." in body
        assert "1 account has signed in so far." in body

        client.cookies.clear()
        sign_in(client)
        assert len(_to(sent)) == 1

        # The CLI names itself in its User-Agent.
        with SessionLocal() as db:
            code = create_login_code(db, "arne@example.com")
        r = client.post(
            "/api/auth/verify-code",
            json={"email": "arne@example.com", "code": code},
            headers={"User-Agent": "meertex-cli/0.9.0"},
        )
        assert r.status_code == 200, r.text
        subject, body = _to(sent)[1]
        assert subject == "New user arne@example.com"
        assert "with the sign-in code from the CLI" in body
        assert "2 accounts have signed in so far." in body


def test_the_mail_names_who_invited_the_new_account(monkeypatch):
    """An invite makes the account at once. Its mail waits for the person's
    first sign-in and says who invited them to which paper."""
    from fastapi.testclient import TestClient

    from app import when
    from app.main import app
    from app.models import utcnow
    from app.routers import members

    invites = []
    monkeypatch.setattr(members, "send_invite", lambda to, inviter, title, role, link: invites.append(link))
    sent = _new_user_mails(monkeypatch)
    with running_app() as client:
        sign_in(client)
        client.patch("/api/auth/me", json={"name": "Didi"})
        green = client.post("/api/projects", json={"name": "green", "title": "Green LLMs"}).json()
        blue = client.post("/api/projects", json={"name": "blue"}).json()
        for project, email, role in ((green, "arne@example.com", "editor"), (blue, "arne@example.com", "viewer"),
                                     (green, "bob@example.com", "reviewer")):
            r = client.post(f"/api/projects/{project['id']}/members", json={"email": email, "role": role})
            assert r.status_code == 201, r.text
        # Inviting mails nobody but the invited.
        assert len(_to(sent)) == 1

        electron = "Mozilla/5.0 (X11; Linux x86_64) meertex/0.4.0 Chrome/140.0 Electron/38.0.0 Safari/537.36"
        with TestClient(app, headers={"User-Agent": electron}) as arne:
            r = arne.get(invites[0].removeprefix("http://testserver"), follow_redirects=False)
            assert r.status_code == 302
            arne.get(invites[1].removeprefix("http://testserver"), follow_redirects=False)
        [_, (subject, body)] = _to(sent)
        today = when.day_text(utcnow())
        assert subject == "New user arne@example.com"
        assert (
            f'Didi <didi@example.com> invited them to "Green LLMs" as editor on {today}.\n'
            f'Didi <didi@example.com> invited them to "blue" as viewer on {today}.'
        ) in body
        assert "Nobody invited them" not in body
        assert "They signed in with the invite link in the desktop app.\n" in body
        assert "2 accounts have signed in so far. 1 invited person has not signed in yet." in body


def test_without_an_address_nobody_is_mailed(monkeypatch):
    """The first sign-in is kept all the same, so turning the mail on later
    does not mail about the accounts from before."""
    from fastapi.testclient import TestClient

    from app.main import app

    sent = _new_user_mails(monkeypatch, to="")
    with running_app() as client:
        sign_in(client)
        assert sent == []
        sent = _new_user_mails(monkeypatch)
        with TestClient(app) as again:
            sign_in(again)
        assert sent == []


def test_the_backfill_keeps_the_accounts_that_signed_in_before():
    """Migration 0003 marks every account that shows it signed in, and leaves
    an invited person who never did waiting."""
    from sqlalchemy import text

    from app.database import engine
    from app.migrations import _users_first_sign_in_backfill

    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "green"}).json()
        for email in ("arne@example.com", "bob@example.com", "cleo@example.com"):
            client.post(f"/api/projects/{p['id']}/members", json={"email": email, "role": "viewer"})
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET first_sign_in_at = NULL"))
            conn.execute(text("UPDATE users SET timezone = 'Europe/Berlin' WHERE email = 'bob@example.com'"))
            conn.execute(text("UPDATE users SET api_token = 'cleos-token' WHERE email = 'cleo@example.com'"))
            assert _users_first_sign_in_backfill(conn)
            rows = conn.execute(text("SELECT email, first_sign_in_at IS NOT NULL FROM users ORDER BY email")).all()
            assert rows == [
                ("arne@example.com", False),
                ("bob@example.com", True),
                ("cleo@example.com", True),
                ("didi@example.com", True),
            ]
            assert not _users_first_sign_in_backfill(conn)
