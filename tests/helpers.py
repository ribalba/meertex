"""Shared test helpers: a running app on a clean database, and signed-in clients."""

import os
import shutil
from contextlib import contextmanager

import pytest
from sqlalchemy import text

needs_db = pytest.mark.skipif(not os.environ.get("MEERTEX_TEST_DB"), reason="MEERTEX_TEST_DB not set")


def truncate() -> None:
    from app.config import get_settings
    from app.database import Base, engine

    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    projects = get_settings().projects_dir
    shutil.rmtree(projects, ignore_errors=True)
    projects.mkdir(parents=True, exist_ok=True)


@contextmanager
def running_app():
    from fastapi.testclient import TestClient

    from app.database import init_db
    from app.main import app

    init_db()
    truncate()
    with TestClient(app) as client:
        yield client


def sign_in(client, email: str = "didi@example.com") -> dict:
    """Sign in through the real code flow and return /api/auth/me."""
    from app.database import SessionLocal
    from app.security import create_login_code

    with SessionLocal() as db:
        code = create_login_code(db, email)
    r = client.post("/api/auth/verify-code", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return client.get("/api/auth/me").json()


def api_token(client) -> str:
    """The signed-in account's API token, created if needed."""
    r = client.post("/api/auth/api-token")
    assert r.status_code == 200, r.text
    return r.json()["api_token"]


@contextmanager
def second_client(email: str):
    """Another signed-in browser on the same app, for sharing tests."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as other:
        sign_in(other, email)
        yield other
