"""Starting a paper from a publisher's template zip (app/importer.py)."""

import io
import stat
import zipfile

from helpers import needs_db, running_app, sign_in

pytestmark = needs_db

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


def _zip(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, target)
    return buf.getvalue()


def _kit() -> bytes:
    # An author kit the way publishers ship them: one wrapping folder, the
    # class's own manual next to the sample paper, macOS leftovers, a figure.
    return _zip({
        "acmart-primary/sample-sigconf.tex": b"\\documentclass[sigconf]{acmart}\n\\begin{document}\nGr\xc3\xbc\xc3\x9fe\n\\end{document}\n",
        "acmart-primary/acmguide.tex": b"\\documentclass{ltxdoc}\n\\begin{document}\nManual\n\\end{document}\n",
        "acmart-primary/acmart.cls": b"% the class\n",
        "acmart-primary/sample-base.bib": b"@article{a, title={A}}\n",
        "acmart-primary/figures/teaser.png": PNG,
        "acmart-primary/sample-sigconf.log": b"build output",
        "__MACOSX/acmart-primary/._acmart.cls": b"junk",
    })


def test_a_kit_becomes_the_projects_files():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "acm-kit"}).json()
        r = client.post(f"/api/projects/{p['id']}/import", content=_kit())
        assert r.status_code == 200, r.text
        out = r.json()
        paths = {f["path"] for f in out["files"]}
        assert paths == {"sample-sigconf.tex", "acmguide.tex", "acmart.cls", "sample-base.bib", "figures/teaser.png"}
        assert {f["path"]: f["kind"] for f in out["files"]}["figures/teaser.png"] == "blob"
        assert {s["reason"] for s in out["skipped"]} == {"a build output", "not part of the template"}
        # The sample paper, not the class's manual.
        assert out["main_file"] == "sample-sigconf.tex" and out["main_file_changed"]
        assert out["main_candidates"] == ["sample-sigconf.tex", "acmguide.tex"]
        assert client.get(f"/api/projects/{p['id']}").json()["main_file"] == "sample-sigconf.tex"
        assert "Grüße" in client.get(f"/api/projects/{p['id']}/files/sample-sigconf.tex").text
        assert client.get(f"/api/projects/{p['id']}/files/figures/teaser.png").content == PNG
        # It is in the history as one snapshot.
        assert client.get(f"/api/projects/{p['id']}/files", params={"ref": "HEAD"}).json()


def test_existing_files_need_replace_and_main_file_stays():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "has-files", "starter": "basic"}).json()
        kit = _zip({"main.tex": b"\\documentclass{article}\n\\begin{document}\nnew\n\\end{document}\n"})
        r = client.post(f"/api/projects/{p['id']}/import", content=kit)
        assert r.status_code == 422 and "main.tex" in r.json()["detail"]
        r = client.post(f"/api/projects/{p['id']}/import", content=kit, params={"replace": "true"})
        assert r.status_code == 200
        assert r.json()["main_file"] == "main.tex" and not r.json()["main_file_changed"]
        assert "new" in client.get(f"/api/projects/{p['id']}/files/main.tex").text


def test_hostile_zips_are_refused_or_cleaned():
    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "hostile"}).json()
        url = f"/api/projects/{p['id']}/import"
        assert client.post(url, content=b"not a zip").status_code == 422
        r = client.post(url, content=_zip(
            {"../../etc/passwd": b"x", "/abs.tex": b"x", "ok.tex": b"fine", ".git/config": b"x"},
            links={"link.tex": "/etc/passwd"},
        ))
        assert r.status_code == 200, r.text
        assert [f["path"] for f in r.json()["files"]] == ["ok.tex"]
        reasons = {s["path"]: s["reason"] for s in r.json()["skipped"]}
        assert reasons["link.tex"] == "a link"
        assert reasons[".git/config"] == "not part of the template"
        assert len([s for s in r.json()["skipped"] if "outside" in s["reason"]]) == 2


def test_only_editors_import():
    from helpers import second_client

    with running_app() as client:
        sign_in(client)
        p = client.post("/api/projects", json={"name": "roles"}).json()
        client.post(f"/api/projects/{p['id']}/members", json={"email": "v@example.com", "role": "viewer"})
        with second_client("v@example.com") as viewer:
            assert viewer.post(f"/api/projects/{p['id']}/import", content=_kit()).status_code == 403
