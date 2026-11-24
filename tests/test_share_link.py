"""Sharing a paper with a link (DESIGN §2, Sharing a paper with a link)."""

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db


def _no_mail(monkeypatch) -> None:
    from app.routers import members

    monkeypatch.setattr(members, "send_invite", lambda *a, **k: None)


def test_a_link_lets_anyone_join_in_its_role():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "wifi", "title": "WiFi Energy"}).json()
        url = f"/api/projects/{p['id']}/share"
        assert client.get(url).json() is None

        r = client.post(url, json={"role": "reviewer"})
        assert r.status_code == 201, r.text
        share = r.json()
        assert set(share) == {"token", "url", "role", "created_by", "created_at"}
        assert share["url"] == f"http://testserver/join/{share['token']}"
        assert share["role"] == "reviewer"
        assert share["created_by"]["email"] == "didi@example.com"
        assert client.get(url).json()["token"] == share["token"]

        page = client.get(f"/join/{share['token']}")
        assert page.status_code == 200
        assert page.headers["x-robots-tag"] == "noindex, nofollow"
        assert page.headers["referrer-policy"] == "no-referrer"

        with second_client("jacob@example.com") as jacob:
            # Not a member yet, so the paper is hidden, but the link names it.
            assert jacob.get(f"/api/projects/{p['id']}").status_code == 404
            preview = jacob.get(f"/api/join/{share['token']}").json()
            assert preview == {
                "project": {"id": p["id"], "name": "wifi", "title": "WiFi Energy"},
                "role": "reviewer",
                "shared_by": {"email": "didi@example.com", "name": preview["shared_by"]["name"]},
                "member_role": None,
            }
            r = jacob.post(f"/api/join/{share['token']}")
            assert r.json() == {"project_id": p["id"], "role": "reviewer", "joined": True}
            assert jacob.get(f"/api/projects/{p['id']}").json()["role"] == "reviewer"
            assert jacob.get(f"/api/join/{share['token']}").json()["member_role"] == "reviewer"

            # A changed role leaves who joined as they are, and joining again changes nothing.
            assert client.patch(url, json={"role": "editor"}).json()["role"] == "editor"
            assert jacob.post(f"/api/join/{share['token']}").json() == {
                "project_id": p["id"], "role": "reviewer", "joined": False,
            }

        # The owner joining their own paper keeps owning it.
        assert client.post(f"/api/join/{share['token']}").json()["role"] == "owner"

        members = client.get(f"/api/projects/{p['id']}/members").json()
        assert [(m["email"], m["role"]) for m in members] == [("didi@example.com", "owner"), ("jacob@example.com", "reviewer")]


def test_a_new_or_stopped_link_stops_the_old_one():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "wifi"}).json()
        url = f"/api/projects/{p['id']}/share"
        first = client.post(url).json()
        assert first["role"] == "viewer"
        second = client.post(url, json={"role": "viewer"}).json()
        assert second["token"] != first["token"]

        with second_client("jacob@example.com") as jacob:
            assert jacob.get(f"/api/join/{first['token']}").status_code == 404
            assert jacob.post(f"/api/join/{first['token']}").status_code == 404
            assert jacob.post(f"/api/join/{second['token']}").json()["joined"] is True

            assert client.delete(url).json() == {"ok": True}
            assert client.get(url).json() is None
            assert client.patch(url, json={"role": "viewer"}).status_code == 404
            assert jacob.get(f"/api/join/{second['token']}").status_code == 404
            # Who joined stays.
            assert jacob.get(f"/api/projects/{p['id']}").json()["role"] == "viewer"


def test_who_may_manage_the_link(monkeypatch):
    _no_mail(monkeypatch)
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "wifi"}).json()
        members = f"/api/projects/{p['id']}/members"
        url = f"/api/projects/{p['id']}/share"
        client.post(members, json={"email": "ed@example.com", "role": "editor"})
        client.post(members, json={"email": "rev@example.com", "role": "reviewer"})

        with second_client("rev@example.com") as rev:
            assert rev.get(url).status_code == 403
            assert rev.post(url, json={"role": "viewer"}).status_code == 403
        with second_client("eve@example.com") as eve:
            assert eve.get(url).status_code == 404
        with second_client("ed@example.com") as ed:
            assert ed.post(url, json={"role": "editor"}).status_code == 403
            assert ed.post(url, json={"role": "reviewer"}).status_code == 201
            assert ed.patch(url, json={"role": "viewer"}).json()["role"] == "viewer"
            assert ed.patch(url, json={"role": "editor"}).status_code == 403

            # A link that makes editors is the owner's.
            assert client.patch(url, json={"role": "editor"}).status_code == 200
            assert ed.get(url).json()["role"] == "editor"
            assert ed.patch(url, json={"role": "viewer"}).status_code == 403
            assert ed.post(url, json={"role": "viewer"}).status_code == 403
            assert ed.delete(url).status_code == 403
            assert client.delete(url).status_code == 200


def test_the_link_goes_with_the_paper():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "wifi"}).json()
        share = client.post(f"/api/projects/{p['id']}/share").json()
        assert client.delete(f"/api/projects/{p['id']}?confirm=wifi").status_code == 200
        with second_client("jacob@example.com") as jacob:
            assert jacob.get(f"/api/join/{share['token']}").status_code == 404
