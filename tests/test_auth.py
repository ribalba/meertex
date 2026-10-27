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
