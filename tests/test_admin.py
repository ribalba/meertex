"""Instance admins and the instance dashboard (DESIGN §18)."""

from datetime import date, timedelta

from helpers import api_token, needs_db, running_app, second_client, sign_in

pytestmark = needs_db


def _today() -> date:
    """The server counts in UTC days."""
    from app.models import utcnow

    return utcnow().date()


def _admins(monkeypatch, emails: str = "didi@example.com") -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "instance_admins", emails)


def test_only_instance_admins_see_the_dashboard(monkeypatch):
    """Everybody else gets 404, as if the route did not exist."""
    _admins(monkeypatch, " Didi@Example.com , someone@example.org")
    with running_app() as client:
        assert sign_in(client)["instance_admin"] is True
        assert client.get("/api/admin/instance").status_code == 200
        with second_client("arne@example.com") as arne:
            assert arne.get("/api/auth/me").json()["instance_admin"] is False
            assert arne.get("/api/admin/instance").status_code == 404
        # The API token works too, for scripts and agents.
        token = api_token(client)
        client.cookies.clear()
        assert client.get("/api/admin/instance").status_code == 401
        r = client.get("/api/admin/instance", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


def test_nobody_is_an_admin_by_default():
    with running_app() as client:
        assert sign_in(client)["instance_admin"] is False
        assert client.get("/api/admin/instance").status_code == 404


def test_the_dashboard_counts_the_instance(monkeypatch):
    from app.routers import members

    monkeypatch.setattr(members, "send_invite", lambda *args: None)
    _admins(monkeypatch)
    with running_app() as client:
        sign_in(client)
        client.patch("/api/auth/me", json={"name": "Didi"})
        green = client.post("/api/projects", json={"name": "green", "title": "Green LLMs"}).json()
        client.post("/api/projects", json={"name": "blue"})
        r = client.post(f"/api/projects/{green['id']}/members", json={"email": "arne@example.com", "role": "editor"})
        assert r.status_code == 201, r.text

        d = client.get("/api/admin/instance").json()
        assert (d["period"], d["unit"], len(d["buckets"])) == ("30d", "day", 30)
        assert d["buckets"][-1] == _today().isoformat()
        assert d["totals"] == {
            "accounts": 2, "signed_in": 1, "waiting": 1, "papers": 2, "accepted": 0, "planned": 0,
            "active_7d": 1, "active_30d": 1,
        }
        assert d["storage"]["database_bytes"] > 0
        assert d["storage"]["files_bytes"] > 0
        series = d["series"]
        assert set(series) == {"active", "accounts", "papers", "snapshots", "compiles", "comments", "reviews", "versions"}
        assert all(len(v) == 30 for v in series.values())
        assert (series["active"][-1], series["accounts"][-1], series["papers"][-1]) == (1, 1, 2)
        # Every new project starts its history with a commit (§9).
        assert series["snapshots"][-1] >= 2
        assert sum(series["active"][:-1]) == 0
        assert d["growth"] == {"accounts": [0] * 29 + [1], "papers": [0] * 29 + [2]}

        assert d["accounts_total"] == 2
        didi, arne = d["accounts"]
        assert (didi["email"], didi["papers"], didi["invited_by"]) == ("didi@example.com", 2, None)
        assert didi["first_sign_in_at"] and didi["last_active"] == _today().isoformat()
        assert (arne["email"], arne["papers"], arne["first_sign_in_at"], arne["last_active"]) == (
            "arne@example.com", 1, None, None)
        assert arne["invited_by"] == {"email": "didi@example.com", "name": "Didi"}


def test_the_periods_cut_weeks_and_months(monkeypatch):
    _admins(monkeypatch)
    with running_app() as client:
        sign_in(client)
        weeks = client.get("/api/admin/instance?period=90d").json()
        assert (weeks["unit"], len(weeks["buckets"])) == ("week", 13)
        assert all(date.fromisoformat(b).weekday() == 0 for b in weeks["buckets"])
        assert weeks["series"]["active"][-1] == 1
        months = client.get("/api/admin/instance?period=12m").json()
        assert (months["unit"], len(months["buckets"])) == ("month", 12)
        assert all(b.endswith("-01") for b in months["buckets"])
        assert months["buckets"][-1] == _today().replace(day=1).isoformat()
        assert client.get("/api/admin/instance?period=7d").status_code == 422


def test_a_used_account_is_kept_as_active_once_a_day():
    """Any signed-in request counts, by session or by token."""
    from sqlalchemy import text

    from app.database import engine

    with running_app() as client:
        me = sign_in(client)
        for _ in range(3):
            client.get("/api/auth/me")
        token = api_token(client)
        client.cookies.clear()
        client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})
        with engine.begin() as conn:
            rows = conn.execute(text("SELECT user_id, day FROM active_days")).all()
        assert rows == [(me["id"], _today())]


def test_the_backfill_finds_the_days_before_it():
    """Migration 0004 takes the days from the rows the accounts left."""
    from sqlalchemy import text

    from app.database import engine
    from app.migrations import _active_days_backfill

    with running_app() as client:
        me = sign_in(client)
        client.post("/api/projects", json={"name": "green"})
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM active_days"))
            conn.execute(text("UPDATE projects SET created_at = created_at - interval '3 days'"))
            conn.execute(text("UPDATE memberships SET added_at = added_at - interval '3 days'"))
            assert _active_days_backfill(conn)
            days = conn.execute(text("SELECT day FROM active_days WHERE user_id = :u ORDER BY day"), {"u": me["id"]})
            assert [d for (d,) in days] == [_today() - timedelta(days=3), _today()]
            assert not _active_days_backfill(conn)


def test_signing_in_counts_as_active():
    """Before any other request, so an account that signed in never reads
    "Never" on the dashboard."""
    from sqlalchemy import text

    from app.database import SessionLocal, engine
    from app.security import create_login_code

    with running_app() as client:
        with SessionLocal() as db:
            code = create_login_code(db, "lena@example.org")
        r = client.post("/api/auth/verify-code", json={"email": "lena@example.org", "code": code})
        assert r.status_code == 200, r.text
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM active_days")).scalar() == 1
