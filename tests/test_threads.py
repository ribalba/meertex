"""Threads and their anchoring (DESIGN §13, Anchoring). A span is found again in
the live text after edits, renames and deletes, in code points and UTF-16 units."""

import json
import shutil
import subprocess

import pytest
from helpers import needs_db, running_app, sign_in
from reviewing import TEXT, js_length, live_text, project_with, put, snapshot, thread

THREAD_KEYS = {
    "id", "review_id", "state", "pending", "created_by", "created_at", "updated_at", "file_id", "path", "start",
    "end", "start16", "end16", "line_start", "line_end", "text", "quote", "changed", "outdated", "base",
    "resolution", "accepted", "comments",
}
COMMENT_KEYS = {"id", "author", "kind", "body", "suggestion", "created_at", "edited_at"}


# --- Without a database: the mapping, lines and the word diff --------------------------


def _anchorer(base: str, live: str):
    """An Anchorer that knows one base text and one live file, without git."""
    from app.anchors import Anchorer, LiveFiles

    a = Anchorer()
    a._base[("p", "c0ffee", "main.tex")] = base
    a.use_live("p", LiveFiles({"f1": ("main.tex", live)}, {"main.tex": "f1"}))
    return a


def _thread_on(base: str, needle: str):
    from app.anchors import lines_of
    from app.models import Thread

    start = base.index(needle)
    end = start + len(needle)
    first, last = lines_of(base, start, end)
    return Thread(id="t", project_id="p", file_id="f1", base_commit="c0ffee", base_path="main.tex",
                  base_start=start, base_end=end, base_line_start=first, base_line_end=last, quote=needle)


@pytest.mark.parametrize(("live", "text", "changed", "outdated"), [
    # Unchanged, and edits before, inside and after the span.
    ("We measure the energy of LLMs.", "the energy", False, False),
    ("Carefully, we measure the energy of LLMs.", "the energy", False, False),
    ("We measure the total energy of LLMs.", "the total energy", True, False),
    # The diff puts " use" after the space, so it is outside the span. The
    # comma is inserted at the span's end, which joins it.
    ("We measure the energy use of LLMs.", "the energy", False, False),
    ("We measure the energy, of LLMs.", "the energy,", True, False),
    ("We measure the energy of all LLMs.", "the energy", False, False),
    # Rewritten words map onto the new words.
    ("We measure a power of LLMs.", "a power", True, False),
    # Deleted as a whole, so outdated.
    ("We measure of LLMs.", "", True, True),
])
def test_mapping(live, text, changed, outdated):
    base = "We measure the energy of LLMs."
    span = _anchorer(base, live).locate(_thread_on(base, "the energy"))
    assert (span.text, span.changed, span.outdated) == (text, changed, outdated)
    if outdated:
        assert (span.start, span.end, span.path) == (11, 21, "main.tex")
    else:
        assert live[span.start:span.end] == text


def test_mapping_counts_utf16_like_javascript():
    base = "Grüße 😀 aus Köln. Wir messen 🔋 Energie."
    live = "🎉🎉 Grüße 😀 aus Köln. Wir messen 🔋 die Energie."
    span = _anchorer(base, live).locate(_thread_on(base, "Energie"))
    assert span.text == "Energie"
    assert span.start == live.index("Energie")
    assert span.start16 == js_length(live[: span.start])
    assert span.end16 - span.start16 == 7
    assert span.start16 - span.start == 4  # three emoji before it, each two units


def test_empty_quote_is_a_point_not_outdated():
    from app.anchors import Anchorer, LiveFiles
    from app.models import Thread

    a = Anchorer()
    a._base[("p", "c", "x.tex")] = "ab"
    a.use_live("p", LiveFiles({"f": ("x.tex", "aXb")}, {"x.tex": "f"}))
    t = Thread(id="t", project_id="p", file_id="f", base_commit="c", base_path="x.tex", base_start=1, base_end=1,
               base_line_start=1, base_line_end=1, quote="")
    span = a.locate(t)
    assert (span.start, span.end, span.outdated, span.changed) == (2, 2, False, False)
    # Both texts empty. diff_xIndex would fail on an empty diff.
    a = Anchorer()
    a._base[("p", "c", "x.tex")] = ""
    a.use_live("p", LiveFiles({"f": ("x.tex", "")}, {"x.tex": "f"}))
    t.base_start = t.base_end = 0
    assert a.locate(t).start == 0


def test_lines():
    from app.anchors import line_count, line_span, lines_of

    text = "one\ntwo\r\nthree\n"
    assert line_count(text) == 3
    assert line_count("") == 1
    assert line_count("no newline") == 1
    start, end = line_span(text, 2, 2)
    assert text[start:end] == "two"  # without \r\n
    start, end = line_span(text, 1, 3)
    assert text[start:end] == "one\ntwo\r\nthree"
    assert lines_of(text, 0, 4) == (1, 1)  # ends on its own newline
    assert lines_of(text, 0, 5) == (1, 2)
    with pytest.raises(ValueError, match="has 3 lines"):
        line_span(text, 3, 4)


def test_word_diff_is_gits_porcelain_with_the_files_lines():
    from app.anchors import word_diff

    assert word_diff("same", "same") == ""
    out = word_diff("We measure the energy.", "We carefully measure the energy.\nAnd more.", 12, 14)
    assert out == "@@ -12 +14,2 @@\n We \n+carefully\n  measure the energy.\n~\n+And more.\n~\n"
    assert word_diff("Grüße", "Viele Grüße").splitlines()[1:] == ["+Viele", "  Grüße", "~"]


# --- Through the API --------------------------------------------------------------------



@needs_db
def test_a_thread_on_live_takes_a_snapshot_and_counts_utf16():
    from app import live

    with running_app() as client:
        sign_in(client)
        p = project_with(client)
        pid = p["id"]
        t = thread(client, pid, "Köln 😀 und Zürich", body="Name the third city.")
        assert set(t) == THREAD_KEYS
        assert set(t["comments"][0]) == COMMENT_KEYS
        assert t["base"]["commit"] == live.resolve_commit(pid, "HEAD")
        assert (t["state"], t["pending"], t["review_id"], t["outdated"], t["changed"]) == ("open", False, None, False, False)
        assert t["quote"] == t["text"] == "Köln 😀 und Zürich"
        assert (t["line_start"], t["line_end"]) == (3, 3)
        start = TEXT.index("Köln")
        assert (t["start"], t["end"]) == (start, start + len("Köln 😀 und Zürich"))
        assert t["start16"] == js_length(TEXT[:start])
        assert t["end16"] == t["start16"] + js_length("Köln 😀 und Zürich")
        assert t["file_id"] == next(f["id"] for f in client.get(f"/api/projects/{pid}/files").json())
        assert t["created_by"]["email"] == "didi@example.com"
        [c] = t["comments"]
        assert (c["kind"], c["body"], c["suggestion"], c["edited_at"]) == ("comment", "Name the third city.", None, None)

        if shutil.which("node"):
            # The editor's numbers, from JavaScript's own String.length and slice.
            current = live_text(pid)
            script = ("const fs=require('fs');const t=fs.readFileSync(0,'utf8');const a=JSON.parse(process.argv[1]);"
                      "process.stdout.write(t.slice(a[0],a[1]))")
            got = subprocess.run(["node", "-e", script, json.dumps([t["start16"], t["end16"]])],
                                 input=current.encode(), capture_output=True, check=True, timeout=30).stdout.decode()
            assert got == t["text"]


@needs_db
def test_the_span_follows_edits_before_inside_and_after():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "die Energie 🔋 von LLMs")
        url = f"/api/projects/{pid}/threads/{t['id']}"

        before = "% Kommentar 🎉\n" + TEXT
        put(client, pid, "main.tex", before)
        got = client.get(url).json()
        assert got["text"] == "die Energie 🔋 von LLMs"
        assert (got["changed"], got["line_start"], got["start"]) == (False, 3, t["start"] + len("% Kommentar 🎉\n"))
        assert got["start16"] == js_length(before[: got["start"]])

        inside = before.replace("Energie 🔋 von", "gesamte Energie 🔋 von allen")
        put(client, pid, "main.tex", inside)
        got = client.get(url).json()
        assert (got["text"], got["changed"], got["outdated"]) == ("die gesamte Energie 🔋 von allen LLMs", True, False)
        assert got["quote"] == "die Energie 🔋 von LLMs"

        after = inside.replace("The end.", "Das Ende.")
        put(client, pid, "main.tex", after)
        assert client.get(url).json()["text"] == "die gesamte Energie 🔋 von allen LLMs"
        assert snapshot(pid)  # a snapshot changes nothing about the span
        assert client.get(url).json()["start"] == after.index("die gesamte")


@needs_db
def test_a_thread_on_an_old_commit_while_the_live_text_moved_on():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        old = snapshot(pid)
        r = client.post(f"/api/projects/{pid}/versions", json={"kind": "submission"})
        assert r.status_code == 201
        moved = "Neu: eine Zeile.\n" + TEXT.replace("Zürich", "Bern")
        put(client, pid, "main.tex", moved)

        for ref in (old, old[:10], "submission"):
            start = TEXT.index("Grüße")
            r = client.post(f"/api/projects/{pid}/threads", json={
                "path": "main.tex", "ref": ref, "start": start, "end": start + len("Grüße aus Köln"), "body": "Ok?"})
            assert r.status_code == 201, r.text
            t = r.json()
            assert t["base"]["commit"] == old
            assert (t["base"]["line_start"], t["line_start"]) == (3, 4)
            assert t["text"] == t["quote"] == "Grüße aus Köln"
            assert t["start"] == moved.index("Grüße")
        # HEAD is still the old commit. The live edit is not snapshotted yet.
        t = client.post(f"/api/projects/{pid}/threads", json={
            "path": "main.tex", "ref": "HEAD", "line_start": 3, "body": "This line."}).json()
        assert (t["base"]["commit"], t["quote"], t["text"]) == (old, "Grüße aus Köln 😀 und Zürich.", "Grüße aus Köln 😀 und Bern.")
        assert t["changed"] is True


@needs_db
def test_a_deleted_span_or_file_is_outdated():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "Grüße aus Köln 😀 und Zürich.")
        url = f"/api/projects/{pid}/threads/{t['id']}"
        put(client, pid, "main.tex", TEXT.replace("Grüße aus Köln 😀 und Zürich.\n", ""))
        got = client.get(url).json()
        assert (got["outdated"], got["changed"], got["text"]) == (True, True, "")
        assert {k: got[k] for k in ("path", "start", "end", "line_start", "line_end")} == {
            "path": "main.tex", "start": t["start"], "end": t["end"], "line_start": 3, "line_end": 3}
        assert got["start16"] == t["start16"]

        # Back again, and found again.
        put(client, pid, "main.tex", TEXT)
        assert client.get(url).json()["outdated"] is False

        assert client.delete(f"/api/projects/{pid}/files/main.tex").status_code == 200
        got = client.get(url).json()
        assert (got["outdated"], got["path"], got["file_id"]) == (True, "main.tex", t["file_id"])


@needs_db
def test_a_renamed_file_is_found_by_its_id():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        t = thread(client, pid, "The end.")
        r = client.post(f"/api/projects/{pid}/files/move", json={"from": "main.tex", "to": "sections/paper.tex"})
        assert r.status_code == 200, r.text
        put(client, pid, "sections/paper.tex", "Vorne.\n" + TEXT)
        got = client.get(f"/api/projects/{pid}/threads/{t['id']}").json()
        assert (got["path"], got["outdated"], got["file_id"], got["line_start"]) == ("sections/paper.tex", False, t["file_id"], 5)
        assert got["base"]["path"] == "main.tex"
        listed = client.get(f"/api/projects/{pid}/threads", params={"path": "sections/paper.tex"}).json()
        assert [x["id"] for x in listed] == [t["id"]]
        assert client.get(f"/api/projects/{pid}/threads", params={"path": "main.tex"}).json() == []

        # A thread made later on the old commit, by the old path, is followed
        # through git's rename detection to the live file.
        old = t["base"]["commit"]
        start = TEXT.index("Grüße")
        r = client.post(f"/api/projects/{pid}/threads", json={
            "path": "main.tex", "ref": old, "start": start, "end": start + 5, "body": "Hallo?"})
        assert r.status_code == 201, r.text
        late = r.json()
        assert (late["file_id"], late["path"], late["outdated"], late["text"]) == (
            t["file_id"], "sections/paper.tex", False, "Grüße")
        # A file deleted since that commit is not followed anywhere.
        assert client.delete(f"/api/projects/{pid}/files/sections/paper.tex").status_code == 200
        put(client, pid, "other.tex", "Etwas anderes.\n")
        r = client.post(f"/api/projects/{pid}/threads", json={
            "path": "main.tex", "ref": old, "start": start, "end": start + 5, "body": "Hallo?"})
        assert (r.json()["file_id"], r.json()["outdated"]) == (None, True)


@needs_db
def test_line_based_anchors_and_bad_spans():
    with running_app() as client:
        sign_in(client)
        pid = project_with(client)["id"]
        url = f"/api/projects/{pid}/threads"

        def make(**body):
            return client.post(url, json={"path": "main.tex", "body": "Hm.", **body})

        t = make(line_start=2, line_end=3).json()
        assert t["quote"] == "Wir messen die Energie 🔋 von LLMs.\nGrüße aus Köln 😀 und Zürich."
        assert (t["line_start"], t["line_end"], t["start"]) == (2, 3, TEXT.index("Wir"))
        assert make(line_start=4).json()["quote"] == "The end."
        for bad in ({"line_start": 5}, {"line_start": 3, "line_end": 2}, {"start": 5, "end": 4},
                    {"start": 0, "end": len(TEXT) + 1}, {"start": 3}, {}):
            r = make(**bad)
            assert r.status_code == 422, (bad, r.text)
        assert make(start=0, end=0, body="", suggestion=None).status_code == 422  # nothing to say
        assert make(start=0, end=0, body="", suggestion="").status_code == 201  # an empty suggestion deletes
        assert make(start=0, end=1, path="nope.tex").status_code == 404
        assert make(start=0, end=1, ref="no-such-tag").status_code == 404
        assert make(start=0, end=1, ref="-x").status_code == 422
        assert make(start=0, end=1, path="../etc/passwd").status_code == 422
        r = client.put(f"/api/projects/{pid}/files/fig.png", content=b"\x89PNG\r\n\x1a\n\x00\x00")
        assert r.status_code == 200
        assert make(start=0, end=1, path="fig.png").status_code == 422
        # Whole lines of a file with Windows line ends leave the \r out.
        put(client, pid, "win.tex", "eins\r\nzwei\r\n")
        assert make(path="win.tex", line_start=1).json()["quote"] == "eins"
