"""Profile pictures (DESIGN §2, Accounts)."""

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db

# Only the first bytes count for the server. The web app sends a real picture.
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
WEBP = b"RIFF\x24\0\0\0WEBPVP8 " + b"\0" * 24


def _files() -> set[str]:
    from app import avatars

    folder = avatars.avatars_dir()
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def test_upload_change_and_remove():
    with running_app() as client:
        me = sign_in(client)
        assert me["avatar_url"] is None
        r = client.put("/api/auth/me/avatar", content=PNG)
        assert r.status_code == 200, r.text
        url = r.json()["avatar_url"]
        assert url.startswith("/api/avatars/") and url.endswith(".png")
        assert client.get("/api/auth/me").json()["avatar_url"] == url

        pic = client.get(url)
        assert pic.status_code == 200
        assert pic.content == PNG
        assert pic.headers["content-type"] == "image/png"
        assert pic.headers["x-content-type-options"] == "nosniff"
        assert "immutable" in pic.headers["cache-control"]

        # A new picture gets a new name, and the old file goes.
        new = client.put("/api/auth/me/avatar", content=WEBP).json()["avatar_url"]
        assert new.endswith(".webp")
        assert client.get(url).status_code == 404
        assert client.get(new).headers["content-type"] == "image/webp"

        r = client.delete("/api/auth/me/avatar")
        assert r.status_code == 200
        assert r.json()["avatar_url"] is None
        assert client.get(new).status_code == 404


def test_only_pictures_and_not_too_large(monkeypatch):
    from app.config import get_settings

    with running_app() as client:
        sign_in(client)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        for body in (svg, b"hello", b""):
            r = client.put("/api/auth/me/avatar", content=body)
            assert r.status_code == 422, body
            assert "PNG, JPEG, GIF or WebP" in r.json()["detail"]
        monkeypatch.setattr(get_settings(), "max_avatar_bytes", 1024)
        r = client.put("/api/auth/me/avatar", content=PNG + b"\0" * 1024)
        assert r.status_code == 413
        assert client.get("/api/auth/me").json()["avatar_url"] is None
        # Names are a hash and a format, nothing that walks out of the folder.
        assert client.get("/api/avatars/..%2F..%2Fsecret.png").status_code == 404
        assert client.get(f"/api/avatars/{'0' * 64}.svg").status_code == 404


def test_coauthors_see_it_and_a_shared_file_stays():
    with running_app() as client:
        sign_in(client)
        url = client.put("/api/auth/me/avatar", content=PNG).json()["avatar_url"]
        p = client.post("/api/projects", json={"name": "green", "title": "Green LLMs"}).json()
        assert p["owner"]["avatar_url"] == url
        with second_client("arne@example.com") as arne:
            r = client.post(f"/api/projects/{p['id']}/members", json={"email": "arne@example.com", "role": "editor"})
            assert r.status_code == 201, r.text
            members = arne.get(f"/api/projects/{p['id']}/members").json()
            assert {m["email"]: m["avatar_url"] for m in members} == {"didi@example.com": url, "arne@example.com": None}
            assert arne.get(url).status_code == 200

            # The same picture on two accounts is one file. It stays while one of them shows it.
            assert arne.put("/api/auth/me/avatar", content=PNG).json()["avatar_url"] == url
            name = url.rsplit("/", 1)[1]
            assert name in _files()
            client.delete("/api/auth/me/avatar")
            assert arne.get(url).status_code == 200
            arne.delete("/api/auth/me/avatar")
            assert name not in _files()

        client.cookies.clear()
        assert client.get(url).status_code == 401
