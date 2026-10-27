"""Versions: numbers, labels, tags, which state is frozen, and the PDF (DESIGN §7)."""

import time

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db

VERSION_KEYS = {
    "id", "number", "kind", "label", "tag", "commit", "date_id", "note", "pdf_name", "created_at", "created_by",
    "compiled",
}
PDF = b"%PDF-1.7\n% fake but plausible\n%%EOF\n"


def _edit(project_id: str, text: str, snapshot: bool = True) -> str | None:
    from app import live
    from app.live import rooms, ydoc

    with rooms.get(project_id).edit("didi@example.com") as doc:
        ydoc.put_text(doc, "main.tex", text)
    return live.snapshot(project_id) if snapshot else None


def test_numbers_labels_and_tags():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "paper", "starter": "basic"}).json()
        url = f"/api/projects/{p['id']}/versions"

        def make(**body) -> dict:
            r = client.post(url, json=body)
            assert r.status_code == 201, r.text
            return r.json()

        first = make(kind="submission", note="HotCRP #123")
        assert set(first) == VERSION_KEYS
        assert (first["number"], first["label"], first["tag"], first["note"]) == (1, "Submission", "submission", "HotCRP #123")
        assert first["pdf_name"] is None
        assert first["created_by"]["email"] == "didi@example.com"
        assert [(v["label"], v["tag"]) for v in (make(kind="revision"), make(kind="revision"))] == [
            ("Revision 1", "revision-1"),
            ("Revision 2", "revision-2"),
        ]
        assert make(kind="submission")["tag"] == "submission-2"
        assert make(kind="snapshot")["label"] == "Snapshot 1"
        assert make(kind="camera_ready")["tag"] == "camera-ready"
        assert make(kind="arxiv")["label"] == "arXiv"
        other = make(kind="other", label="Workshop Version für ÖPNV")
        assert other["tag"] == "workshop-version-fuer-oepnv"
        assert other["number"] == 8
        assert client.post(url, json={"kind": "other"}).status_code == 422
        assert client.post(url, json={"kind": "draft"}).status_code == 422
        # "main" is the branch, so a version labelled main gets another tag.
        assert make(kind="other", label="main")["tag"] == "main-2"

        listed = client.get(url).json()
        assert [v["number"] for v in listed] == list(range(1, 10))


def test_which_state_is_frozen():
    from app import live
    from app.models import utcnow

    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "paper"}).json()
        pid = p["id"]
        url = f"/api/projects/{pid}/versions"
        early = _edit(pid, "early")
        moment = utcnow()
        time.sleep(1.1)
        late = _edit(pid, "late")
        # Live edits not yet in a commit are taken by "now".
        _edit(pid, "unsaved", snapshot=False)

        now = client.post(url, json={"kind": "snapshot"}).json()
        assert now["commit"] not in (early, late)
        assert now["commit"] == live.resolve_commit(pid, "HEAD")
        assert client.post(url, json={"kind": "snapshot", "at": early[:10]}).json()["commit"] == early
        stamp = moment.isoformat() + "Z"
        assert client.post(url, json={"kind": "snapshot", "at": stamp}).json()["commit"] == early
        assert client.post(url, json={"kind": "snapshot", "at": "HEAD"}).json()["commit"] == now["commit"]
        assert client.post(url, json={"kind": "snapshot", "at": "2000-01-01T00:00:00Z"}).status_code == 422
        assert client.post(url, json={"kind": "snapshot", "at": "yesterday"}).status_code == 422
        assert client.post(url, json={"kind": "snapshot", "at": "deadline"}).status_code == 422

        date = client.post(f"/api/projects/{pid}/dates", json={"kind": "submission", "due_at": stamp}).json()
        v = client.post(url, json={"kind": "submission", "at": "deadline", "date_id": date["id"]}).json()
        assert v["commit"] == early
        assert v["date_id"] == date["id"]
        assert late != early


def test_read_edit_and_delete():
    from app import live

    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "paper", "starter": "basic"}).json()
        url = f"/api/projects/{p['id']}/versions"
        v = client.post(url, json={"kind": "submission"}).json()

        by_tag = client.get(f"{url}/submission").json()
        assert by_tag["id"] == v["id"]
        assert sorted(f["path"] for f in by_tag["files"]) == ["main.tex", "refs.bib"]
        assert by_tag["files"][0]["id"] is None
        assert client.get(f"{url}/{v['id']}").json()["tag"] == "submission"
        assert client.get(f"{url}/nope").status_code == 404

        r = client.patch(f"{url}/submission", json={"label": "Full paper", "note": "final", "tag": "x", "commit": "y"})
        assert r.status_code == 200
        assert (r.json()["label"], r.json()["note"], r.json()["tag"], r.json()["commit"]) == (
            "Full paper",
            "final",
            "submission",
            v["commit"],
        )
        assert client.patch(f"{url}/submission", json={"label": " "}).status_code == 422

        client.post(f"/api/projects/{p['id']}/members", json={"email": "ed@example.com", "role": "editor"})
        with second_client("ed@example.com") as ed:
            assert ed.patch(f"{url}/submission", json={"note": "by ed"}).status_code == 200
            assert ed.delete(f"{url}/submission").status_code == 403
        assert client.delete(f"{url}/submission").status_code == 200
        assert client.get(f"{url}/submission").status_code == 404
        assert live.resolve_commit(p["id"], "submission") is None
        # The commit stays in the history.
        assert live.resolve_commit(p["id"], v["commit"]) == v["commit"]
        # The tag is free again.
        assert client.post(url, json={"kind": "submission"}).json()["tag"] == "submission"


def test_pdf_upload_and_download():
    from app.config import get_settings

    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "paper"}).json()
        url = f"/api/projects/{p['id']}/versions"
        client.post(url, json={"kind": "submission"})

        assert client.get(f"{url}/submission/pdf").status_code == 404
        r = client.put(f"{url}/submission/pdf", content=b"<html>not a pdf</html>")
        assert r.status_code == 422
        r = client.put(f"{url}/submission/pdf", content=PDF)
        assert r.status_code == 200, r.text
        assert r.json()["pdf_name"] == "paper-submission.pdf"
        r = client.get(f"{url}/submission/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content == PDF

        r = client.put(f"{url}/submission/pdf", params={"name": "../icse27-final.pdf"}, content=PDF)
        assert r.json()["pdf_name"] == "..icse27-final.pdf"
        assert "icse27-final.pdf" in client.get(f"{url}/submission/pdf").headers["content-disposition"]

        settings = get_settings()
        limit = settings.max_blob_bytes
        settings.max_blob_bytes = 10
        try:
            assert client.put(f"{url}/submission/pdf", content=PDF).status_code == 413
        finally:
            settings.max_blob_bytes = limit

        client.post(f"/api/projects/{p['id']}/members", json={"email": "vic@example.com", "role": "viewer"})
        with second_client("vic@example.com") as vic:
            assert vic.get(f"{url}/submission/pdf").content == PDF
            assert vic.put(f"{url}/submission/pdf", content=PDF).status_code == 403
            assert vic.post(url, json={"kind": "snapshot"}).status_code == 403
        with second_client("eve@example.com") as eve:
            assert eve.get(f"{url}/submission/pdf").status_code == 404
            assert eve.put(f"{url}/submission/pdf", content=PDF).status_code == 404
