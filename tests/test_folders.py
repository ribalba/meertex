"""Personal folder trees (DESIGN §3)."""

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db


def _folder(client, name: str, **body) -> dict:
    r = client.post("/api/folders", json={"name": name, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_tree_paths_and_order():
    with running_app() as client:
        sign_in(client)
        y2027 = _folder(client, "2027")
        sub = _folder(client, "Submitted", parent="2027")
        assert sub["parent_id"] == y2027["id"]
        assert sub["path"] == "2027/Submitted"
        # A missing parent path is made on the way, like mkdir -p.
        acc = _folder(client, "Accepted", parent="2028/Camera")
        assert acc["path"] == "2028/Camera/Accepted"
        early = _folder(client, "Early", parent_id=y2027["id"])
        assert early["position"] > sub["position"]

        listed = client.get("/api/folders").json()
        assert [f["path"] for f in listed] == [
            "2027",
            "2027/Submitted",
            "2027/Early",
            "2028",
            "2028/Camera",
            "2028/Camera/Accepted",
        ]
        assert set(listed[0]) == {"id", "name", "parent_id", "path", "position"}

        # Reorder siblings by position.
        r = client.patch(f"/api/folders/{early['id']}", json={"position": 0})
        assert r.status_code == 200
        assert [f["name"] for f in client.get("/api/folders").json()][:3] == ["2027", "Early", "Submitted"]


def test_names_unique_among_siblings_ignoring_case():
    with running_app() as client:
        sign_in(client)
        top = _folder(client, "Papers")
        assert client.post("/api/folders", json={"name": "papers"}).status_code == 409
        assert client.post("/api/folders", json={"name": "a/b"}).status_code == 422
        assert client.post("/api/folders", json={"name": "  "}).status_code == 422
        # The same name under another parent is fine.
        inner = _folder(client, "papers", parent_id=top["id"])
        other = _folder(client, "Other")
        assert client.patch(f"/api/folders/{other['id']}", json={"name": "PAPERS"}).status_code == 409
        # Moving next to a sibling of the same name clashes too.
        assert client.patch(f"/api/folders/{inner['id']}", json={"parent_id": None}).status_code == 409
        r = client.patch(f"/api/folders/{other['id']}", json={"name": "Elsewhere"})
        assert r.status_code == 200
        assert r.json()["path"] == "Elsewhere"


def test_moves_and_deletes():
    with running_app() as client:
        sign_in(client)
        a = _folder(client, "A")
        b = _folder(client, "B", parent_id=a["id"])
        c = _folder(client, "C", parent_id=b["id"])
        assert client.patch(f"/api/folders/{a['id']}", json={"parent_id": c["id"]}).status_code == 422
        assert client.patch(f"/api/folders/{a['id']}", json={"parent_id": a["id"]}).status_code == 422
        r = client.patch(f"/api/folders/{c['id']}", json={"parent_id": None})
        assert r.json()["path"] == "C"
        r = client.patch(f"/api/folders/{c['id']}", json={"parent_id": b["id"]})
        assert r.json()["path"] == "A/B/C"

        # Not empty: a subfolder, then a project.
        assert client.delete(f"/api/folders/{b['id']}").status_code == 409
        assert client.delete(f"/api/folders/{c['id']}").status_code == 200
        project = client.post("/api/projects", json={"name": "p1", "folder": "A/B"}).json()
        assert project["folder_id"] == b["id"]
        assert project["folder_path"] == "A/B"
        assert client.delete(f"/api/folders/{b['id']}").status_code == 409
        r = client.put(f"/api/projects/{project['id']}/placement", json={"folder_id": None})
        assert r.status_code == 200
        assert r.json()["folder_id"] is None
        assert r.json()["folder_path"] == ""
        assert client.delete(f"/api/folders/{b['id']}").status_code == 200
        assert client.delete(f"/api/folders/{b['id']}").status_code == 404


def test_folders_are_personal():
    with running_app() as client:
        sign_in(client)
        mine = _folder(client, "Mine")
        with second_client("arne@example.com") as other:
            assert other.get("/api/folders").json() == []
            assert other.patch(f"/api/folders/{mine['id']}", json={"name": "x"}).status_code == 404
            assert other.delete(f"/api/folders/{mine['id']}").status_code == 404
            assert other.post("/api/folders", json={"name": "x", "parent_id": mine["id"]}).status_code == 404
            assert other.post("/api/projects", json={"name": "p", "folder": mine["id"]}).status_code == 404


def test_shared_project_sits_in_each_users_own_folder():
    with running_app() as client:
        sign_in(client)
        project = client.post("/api/projects", json={"name": "shared", "folder": "2027/Accepted"}).json()
        r = client.post(f"/api/projects/{project['id']}/members", json={"email": "arne@example.com", "role": "editor"})
        assert r.status_code == 201
        with second_client("arne@example.com") as other:
            seen = other.get("/api/projects").json()
            assert [(p["name"], p["folder_path"]) for p in seen] == [("shared", "")]
            collab = other.post("/api/folders", json={"name": "Collaborations"}).json()
            r = other.put("/api/projects/shared/placement", json={"folder_id": collab["id"]})
            assert r.json()["folder_path"] == "Collaborations"
            # Placing it into someone else's folder is not possible.
            mine = client.get("/api/folders").json()[0]
            assert other.put("/api/projects/shared/placement", json={"folder_id": mine["id"]}).status_code == 404
        assert client.get("/api/projects/shared").json()["folder_path"] == "2027/Accepted"


def test_list_by_folder_includes_subfolders_in_tree_order():
    with running_app() as client:
        sign_in(client)
        client.post("/api/projects", json={"name": "deep", "folder": "2027/Submitted"})
        client.post("/api/projects", json={"name": "top-b", "folder": "2027"})
        client.post("/api/projects", json={"name": "top-a", "folder": "2027"})
        client.post("/api/projects", json={"name": "outside"})
        names = [p["name"] for p in client.get("/api/projects", params={"folder": "2027"}).json()]
        assert names == ["top-b", "top-a", "deep"]
        sub = client.get("/api/folders").json()[1]
        assert [p["name"] for p in client.get("/api/projects", params={"folder": sub["id"]}).json()] == ["deep"]
        assert client.get("/api/projects", params={"folder": "nope"}).status_code == 404
