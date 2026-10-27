"""The history of a project's snapshots (docs/DESIGN.md §9, §11)."""

import re

from helpers import api_token, needs_db, running_app, sign_in
from test_live_helpers import bare_client, make_project, make_user

pytestmark = needs_db

ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def test_history_lists_snapshots_newest_first():
    from app import live

    with running_app() as client:
        me = sign_in(client)
        bob_id, bob, bob_token = make_user()
        _stranger_id, _stranger, stranger_token = make_user()
        pid, _name = make_project(me["id"], {bob_id: "viewer"})
        base = f"/api/projects/{pid}"

        client.put(f"{base}/files/main.tex", content=b"one")
        client.put(f"{base}/files/refs.bib", content=b"")
        first = live.snapshot(pid)
        client.put(f"{base}/files/main.tex", content=b"two")
        second = live.snapshot(pid, [bob])
        # A snapshot with nothing new makes no commit.
        assert live.snapshot(pid) == second

        r = client.get(f"{base}/history")
        assert r.status_code == 200, r.text
        commits = r.json()
        assert [c["commit"] for c in commits[:2]] == [second, first]
        assert commits[-1]["message"] == "Project created"
        assert commits[-1]["editors"] == []
        assert commits[-1]["files_changed"] == 0
        assert commits[0]["editors"] == sorted([me["email"], bob])
        assert commits[0]["message"] == "Edits by " + ", ".join(sorted([me["email"], bob]))
        assert commits[0]["files_changed"] == 1
        assert commits[1]["editors"] == [me["email"]]
        assert commits[1]["files_changed"] == 2
        assert all(ISO_Z.match(c["at"]) for c in commits)

        assert [c["commit"] for c in client.get(f"{base}/history?limit=1").json()] == [second]
        assert client.get(f"{base}/history?limit=0").status_code == 422

        bare = bare_client()
        assert len(bare.get(f"{base}/history", headers={"Authorization": f"Bearer {bob_token}"}).json()) == 3
        assert bare.get(f"{base}/history", headers={"Authorization": f"Bearer {stranger_token}"}).status_code == 404


def test_commits_name_their_parent():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "parents", "starter": "basic"}).json()
        token = api_token(client)
        commits = client.get(f"/api/projects/{p['id']}/history", headers={"Authorization": f"Bearer {token}"}).json()
        assert [c["message"] for c in commits] == ["Starter files", "Project created"]
        assert commits[0]["parent"] == commits[1]["commit"]
        assert commits[1]["parent"] is None
