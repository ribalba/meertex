"""The reconcile engine without a network (docs/DESIGN.md §10).

A checkout folder in tmp_path with its engine and document, and a second
document standing in for the server. ``exchange`` plays the role of the
y-websocket connection by swapping the missing updates both ways.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from meertex import ydoc as y
from meertex.engine import INDEX_FILE, STATE_FILE, Engine
from pycrdt import Doc


class FakeBlobs:
    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.online = True
        self.uploads = 0
        self.downloads = 0

    async def upload(self, sha: str, data: bytes) -> None:
        if not self.online:
            raise ConnectionError("offline")
        assert hashlib.sha256(data).hexdigest() == sha
        self.uploads += 1
        self.data[sha] = data

    async def download(self, sha: str) -> bytes:
        if not self.online:
            raise ConnectionError("offline")
        self.downloads += 1
        return self.data[sha]


class Server:
    """The other side: one document, and the blobs."""

    def __init__(self):
        self.doc = Doc()
        self.blobs = FakeBlobs()

    def exchange(self, engine: Engine) -> None:
        engine.doc.apply_update(self.doc.get_update(engine.doc.get_state()))
        self.doc.apply_update(engine.doc.get_update(self.doc.get_state()))

    def files(self) -> dict[str, y.Entry]:
        return y.materialize(self.doc)

    def text(self, path: str) -> str:
        return self.files()[path].text

    def put_blob(self, path: str, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self.blobs.data[sha] = data
        y.put_blob(self.doc, path, sha, len(data))
        return sha


def run(coro):
    return asyncio.run(coro)


def sync(engine: Engine, server: Server, paths=None):
    """One round as the client does it while connected: remote updates in,
    reconcile, our updates out."""
    server.exchange(engine)
    report = run(engine.reconcile(paths))
    server.exchange(engine)
    return report


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def make(tmp_path, server):
    def factory(name: str = "co", main_file: str = "main.tex") -> Engine:
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        engine = Engine(root, main_file=main_file, blobs=server.blobs)
        engine.load()
        return engine

    return factory


def write(root: Path, rel: str, text: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


# --- Text ------------------------------------------------------------------------


def test_new_local_file_reaches_the_document(make, server):
    co = make()
    write(co.root, "main.tex", "Hello\n")
    report = sync(co, server)
    assert report.added == ["main.tex"]
    assert server.text("main.tex") == "Hello\n"
    assert (co.root / ".meertex" / STATE_FILE).exists()


def test_local_edit(make, server):
    co = make()
    write(co.root, "main.tex", "We measure energy.\n")
    sync(co, server)
    write(co.root, "main.tex", "We measure the energy.\n")
    report = sync(co, server)
    assert report.sent == ["main.tex"]
    assert server.text("main.tex") == "We measure the energy.\n"


def test_remote_edit(make, server):
    co = make()
    write(co.root, "main.tex", "one\n")
    sync(co, server)
    y.put_text(server.doc, "main.tex", "one\ntwo\n")
    report = sync(co, server)
    assert report.written == ["main.tex"]
    assert read(co.root, "main.tex") == "one\ntwo\n"
    # Our own write is not a local change afterwards.
    again = sync(co, server)
    assert again.empty()


def test_both_edit_the_same_paragraph(make, server):
    co = make()
    write(co.root, "main.tex", "We measure the energy of LLM inference.")
    sync(co, server)
    y.put_text(server.doc, "main.tex", "We measure the energy of LLM inference on GPUs.")
    write(co.root, "main.tex", "We carefully measure the energy of LLM inference.")
    report = sync(co, server)
    merged = "We carefully measure the energy of LLM inference on GPUs."
    assert report.merged == ["main.tex"]
    assert read(co.root, "main.tex") == merged
    assert server.text("main.tex") == merged


def test_umlauts_and_emoji(make, server):
    co = make()
    write(co.root, "intro.tex", "Grüße 😀 aus Köln\n")
    sync(co, server)
    y.put_text(server.doc, "intro.tex", "Grüße 😀 aus Köln und Düsseldorf\n")
    write(co.root, "intro.tex", "Schöne Grüße 😀🎉 aus Köln\n")
    sync(co, server)
    expected = "Schöne Grüße 😀🎉 aus Köln und Düsseldorf\n"
    assert read(co.root, "intro.tex") == expected
    assert server.text("intro.tex") == expected


def test_two_checkouts_converge(make, server):
    a, b = make("a"), make("b")
    write(a.root, "main.tex", "alpha beta gamma\n")
    sync(a, server)
    sync(b, server)
    assert read(b.root, "main.tex") == "alpha beta gamma\n"
    write(a.root, "main.tex", "ALPHA beta gamma\n")
    write(b.root, "main.tex", "alpha beta GAMMA\n")
    sync(a, server)
    sync(b, server)
    sync(a, server)
    assert read(a.root, "main.tex") == read(b.root, "main.tex") == "ALPHA beta GAMMA\n"


# --- Files coming and going ------------------------------------------------------


def test_local_delete_removes_the_entry(make, server):
    co = make()
    write(co.root, "a.tex", "a")
    sync(co, server)
    (co.root / "a.tex").unlink()
    report = sync(co, server)
    assert report.deleted == ["a.tex"]
    assert "a.tex" not in server.files()


def test_local_rename_keeps_the_id(make, server):
    co = make()
    write(co.root, "sections/intro.tex", "Intro text\n")
    sync(co, server)
    old_id = server.files()["sections/intro.tex"].id
    os.rename(co.root / "sections/intro.tex", co.root / "introduction.tex")
    report = sync(co, server)
    assert report.renamed == [["sections/intro.tex", "introduction.tex"]]
    assert server.files()["introduction.tex"].id == old_id
    assert not (co.root / "sections").exists() or not any((co.root / "sections").iterdir())


def test_remote_new_file_and_folder(make, server):
    co = make()
    y.put_text(server.doc, "sections/related.tex", "Related work\n")
    sync(co, server)
    assert read(co.root, "sections/related.tex") == "Related work\n"


def test_remote_rename_moves_the_file(make, server):
    co = make()
    write(co.root, "old.tex", "content\n")
    sync(co, server)
    fid = server.files()["old.tex"].id
    y.rename(server.doc, fid, "new/name.tex")
    report = sync(co, server)
    assert not (co.root / "old.tex").exists()
    assert read(co.root, "new/name.tex") == "content\n"
    assert report.removed == ["old.tex"] and report.trashed == []


def test_remote_rename_with_local_edit_keeps_the_edit(make, server):
    co = make()
    write(co.root, "old.tex", "one two three\n")
    sync(co, server)
    y.rename(server.doc, server.files()["old.tex"].id, "new.tex")
    write(co.root, "old.tex", "one 2 three\n")
    sync(co, server)
    assert not (co.root / "old.tex").exists()
    assert read(co.root, "new.tex") == "one 2 three\n"
    assert server.text("new.tex") == "one 2 three\n"


def test_remote_delete_goes_to_trash(make, server):
    co = make()
    write(co.root, "notes/todo.tex", "todo\n")
    sync(co, server)
    y.remove(server.doc, server.files()["notes/todo.tex"].id)
    report = sync(co, server)
    assert report.trashed == ["notes/todo.tex"]
    assert not (co.root / "notes").exists()
    trashed = list((co.root / ".meertex" / "trash").glob("*/notes/todo.tex"))
    assert len(trashed) == 1 and trashed[0].read_text() == "todo\n"


def test_remote_delete_of_a_locally_changed_file_re_adds_it(make, server):
    co = make()
    write(co.root, "a.tex", "first\n")
    sync(co, server)
    old_id = server.files()["a.tex"].id
    y.remove(server.doc, old_id)
    write(co.root, "a.tex", "first, and more\n")
    report = sync(co, server)
    assert report.added == ["a.tex"]
    assert server.text("a.tex") == "first, and more\n"
    assert server.files()["a.tex"].id != old_id
    assert read(co.root, "a.tex") == "first, and more\n"


def test_local_delete_loses_against_a_remote_edit(make, server):
    co = make()
    write(co.root, "a.tex", "v1\n")
    sync(co, server)
    y.put_text(server.doc, "a.tex", "v2\n")
    (co.root / "a.tex").unlink()
    sync(co, server)
    assert read(co.root, "a.tex") == "v2\n"
    assert server.text("a.tex") == "v2\n"


def test_path_clash_writes_a_conflict_copy(make, server):
    co = make()
    y.add_text(server.doc, "intro.tex", "theirs\n", file_id="f" * 32)
    write(co.root, "intro.tex", "ours\n")
    run(co.reconcile())  # offline, so our file becomes an entry of its own
    ours = y.materialize(co.doc)["intro.tex"].id
    sync(co, server)
    files = server.files()
    winner, loser = sorted([ours, "f" * 32])
    assert files["intro.tex"].id == winner
    conflict = y.conflict_path("intro.tex", loser)
    assert files[conflict].id == loser
    assert {read(co.root, "intro.tex"), read(co.root, conflict)} == {"ours\n", "theirs\n"}
    assert sync(co, server).empty()


def test_watcher_paths_and_folders(make, server):
    co = make()
    write(co.root, "fig/a.tex", "a")
    write(co.root, "fig/b.tex", "b")
    sync(co, server)
    os.rename(co.root / "fig", co.root / "figures")
    # The watcher reports only the two folders.
    report = sync(co, server, paths={"fig", "figures"})
    assert sorted(report.renamed) == [["fig/a.tex", "figures/a.tex"], ["fig/b.tex", "figures/b.tex"]]
    assert sorted(server.files()) == ["figures/a.tex", "figures/b.tex"]


# --- Ignore rules ----------------------------------------------------------------


def test_ignore_rules(make, server):
    co = make(main_file="paper.tex")
    write(co.root, "paper.tex", "x")
    write(co.root, "paper.pdf", b"%PDF-1.5 built")
    write(co.root, "paper.aux", "aux")
    write(co.root, "sub/paper.pdf", b"%PDF-1.5 a figure")
    write(co.root, "node_modules/x/index.js", "js")
    write(co.root, ".git/HEAD", "ref")
    write(co.root, "data/raw.csv", "1,2,3")
    write(co.root, "notes.txt~", "backup")
    write(co.root, ".meertexignore", "data/\n")
    sync(co, server)
    assert sorted(server.files()) == [".meertexignore", "paper.tex", "sub/paper.pdf"]


def test_ignored_folders_are_not_read(make, server, monkeypatch):
    co = make()
    write(co.root, ".meertexignore", "data/\n")
    for i in range(20):
        write(co.root, f"data/file{i}.csv", "x")
    write(co.root, "main.tex", "x")
    seen = []
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            seen.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", walk)
    sync(co, server)
    assert not any(d.endswith("data") for d in seen)


def test_ignore_file_change_applies_right_away(make, server):
    co = make()
    write(co.root, "main.tex", "x")
    sync(co, server)
    y.put_text(server.doc, ".meertexignore", "scratch/\n")
    sync(co, server)
    write(co.root, "scratch/notes.tex", "private")
    sync(co, server)
    assert "scratch/notes.tex" not in server.files()


def test_remote_files_are_written_even_when_ignored(make, server):
    co = make()
    write(co.root, "main.tex", "x")
    sync(co, server)
    y.put_text(server.doc, "build.log", "someone pushed a log\n")
    sync(co, server)
    assert read(co.root, "build.log") == "someone pushed a log\n"
    # A local change to it stays local.
    write(co.root, "build.log", "my own log\n")
    sync(co, server)
    assert server.text("build.log") == "someone pushed a log\n"
    assert read(co.root, "build.log") == "my own log\n"


def test_unchanged_files_are_not_read_again(make, server, monkeypatch):
    co = make()
    write(co.root, "main.tex", "x")
    sync(co, server)
    # Age the recorded stat so the racy-clean check does not apply.
    for shadow in co.shadows.values():
        shadow.seen_ns += 10 * 10**9
    reads = []
    real = Path.read_bytes

    def counting(self):
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert sync(co, server).empty()
    assert reads == []


def test_large_files_are_skipped(make, server, monkeypatch):
    import meertex.engine as engine_mod

    monkeypatch.setattr(engine_mod, "MAX_BLOB_BYTES", 10)
    co = make()
    write(co.root, "big.bin", b"\x00" * 20)
    report = sync(co, server)
    assert "big.bin" not in server.files()
    assert any("big.bin" in w for w in report.warnings)


# --- Blobs -------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def test_local_blob_is_uploaded_first(make, server):
    co = make()
    write(co.root, "fig/plot.png", PNG)
    sync(co, server)
    entry = server.files()["fig/plot.png"]
    assert entry.kind == "blob" and entry.size == len(PNG)
    assert server.blobs.data[entry.sha] == PNG


def test_remote_blob_is_downloaded(make, server):
    co = make()
    server.put_blob("fig/plot.png", PNG)
    sync(co, server)
    assert (co.root / "fig/plot.png").read_bytes() == PNG
    assert server.blobs.downloads == 1
    assert not list((co.root / ".meertex" / "tmp").glob("blob-*"))


def test_remote_blob_rename_copies_locally(make, server):
    co = make()
    write(co.root, "a.png", PNG)
    sync(co, server)
    y.rename(server.doc, server.files()["a.png"].id, "b.png")
    sync(co, server)
    assert (co.root / "b.png").read_bytes() == PNG and not (co.root / "a.png").exists()
    assert server.blobs.downloads == 0


def test_blob_upload_waits_for_the_server(make, server):
    co = make()
    server.blobs.online = False
    write(co.root, "plot.png", PNG)
    report = sync(co, server)
    assert report.pending and "plot.png" not in server.files()
    server.blobs.online = True
    report = sync(co, server)
    assert not report.pending and server.files()["plot.png"].kind == "blob"


def test_blob_last_writer_wins(make, server):
    co = make()
    write(co.root, "a.png", PNG)
    sync(co, server)
    server.put_blob("a.png", PNG + b"remote")
    sync(co, server)
    assert (co.root / "a.png").read_bytes() == PNG + b"remote"
    write(co.root, "a.png", PNG + b"local")
    sync(co, server)
    assert server.blobs.data[server.files()["a.png"].sha] == PNG + b"local"


# --- Restarts --------------------------------------------------------------------


def test_restart_catches_edits_made_while_stopped(make, server):
    co = make()
    write(co.root, "main.tex", "We measure energy.\n")
    write(co.root, "gone.tex", "bye\n")
    write(co.root, "old.tex", "moving\n")
    sync(co, server)
    # Stopped. Meanwhile these changes happen here and on the server.
    write(co.root, "main.tex", "We carefully measure energy.\n")
    (co.root / "gone.tex").unlink()
    os.rename(co.root / "old.tex", co.root / "moved.tex")
    write(co.root, "new.tex", "new\n")
    y.put_text(server.doc, "main.tex", "We measure energy on GPUs.\n")
    # Started again: a fresh engine on the same folder, offline first.
    restarted = make()
    assert restarted.shadows.keys() == {"main.tex", "gone.tex", "old.tex"}
    run(restarted.reconcile())
    sync(restarted, server)
    assert server.text("main.tex") == "We carefully measure energy on GPUs.\n"
    assert read(restarted.root, "main.tex") == "We carefully measure energy on GPUs.\n"
    assert sorted(server.files()) == ["main.tex", "moved.tex", "new.tex"]


def test_restart_does_not_mistake_a_pending_download_for_an_edit(make, server):
    co = make()
    write(co.root, "a.png", PNG)
    sync(co, server)
    server.put_blob("a.png", PNG + b"new")
    server.blobs.online = False
    report = sync(co, server)  # the document knows the new blob but the disk does not yet
    assert report.pending
    restarted = make()
    server.blobs.online = True
    sync(restarted, server)
    assert (restarted.root / "a.png").read_bytes() == PNG + b"new"
    assert server.blobs.data[server.files()["a.png"].sha] == PNG + b"new"


def test_index_holds_shadows(make, server):
    co = make()
    write(co.root, "main.tex", "x")
    sync(co, server)
    index = json.loads((co.root / ".meertex" / INDEX_FILE).read_text())
    rec = index["files"]["main.tex"]
    assert rec["kind"] == "text" and rec["sha"] == hashlib.sha256(b"x").hexdigest()
    assert "text" not in rec  # the saved document holds it


def test_remote_changes_are_observed_after_garbage_collection(make, server):
    import gc

    co = make()
    seen = []
    co.observe(lambda: seen.append("remote"), lambda: seen.append("local"))
    gc.collect()
    y.put_text(server.doc, "a.tex", "from the server")
    server.exchange(co)
    assert "remote" in seen
    write(co.root, "b.tex", "ours")
    run(co.reconcile())
    assert "local" in seen


# --- Holding (§10) -------------------------------------------------------------------


def hold(engine: Engine) -> None:
    from meertex import config

    config.set_hold(engine.root)


def release(engine: Engine) -> None:
    from meertex import config

    config.clear_hold(engine.root)


def test_held_text_edit_stays_out_of_the_document(make, server):
    co = make()
    write(co.root, "main.tex", "one\n")
    sync(co, server)
    hold(co)
    write(co.root, "main.tex", "one\ntwo\n")
    report = sync(co, server)
    assert report.sent == [] and report.empty()
    assert server.text("main.tex") == "one\n"
    assert y.materialize(co.doc)["main.tex"].text == "one\n"  # not even in our own copy
    assert read(co.root, "main.tex") == "one\ntwo\n"
    assert co.held and co.held_paths == {"main.tex"}
    assert co.local_changes() == ["main.tex"]
    release(co)
    report = sync(co, server)
    assert report.sent == ["main.tex"] and server.text("main.tex") == "one\ntwo\n"
    assert not co.held and co.held_paths == set()


def test_held_checkout_still_receives_other_files(make, server):
    co = make()
    write(co.root, "a.tex", "a\n")
    write(co.root, "b.tex", "b\n")
    sync(co, server)
    hold(co)
    write(co.root, "a.tex", "a, rewritten\n")
    y.put_text(server.doc, "b.tex", "b, from a coauthor\n")
    y.put_text(server.doc, "c.tex", "c, new from a coauthor\n")
    report = sync(co, server)
    assert sorted(report.written) == ["b.tex", "c.tex"]
    assert read(co.root, "b.tex") == "b, from a coauthor\n" and read(co.root, "c.tex") == "c, new from a coauthor\n"
    assert server.text("a.tex") == "a\n" and read(co.root, "a.tex") == "a, rewritten\n"


def test_held_and_remote_edit_of_one_file_keeps_both(make, server):
    co = make()
    write(co.root, "para.tex", "We measure the energy of LLM inference.\n")
    sync(co, server)
    hold(co)
    write(co.root, "para.tex", "We carefully measure the energy of LLM inference.\n")
    y.put_text(server.doc, "para.tex", "We measure the energy of LLM inference on GPUs.\n")
    report = sync(co, server)
    both = "We carefully measure the energy of LLM inference on GPUs.\n"
    assert report.kept == ["para.tex"] and read(co.root, "para.tex") == both
    assert server.text("para.tex") == "We measure the energy of LLM inference on GPUs.\n"
    assert co.shadows["para.tex"].content.text == "We measure the energy of LLM inference on GPUs.\n"
    assert sync(co, server).empty()  # nothing new, and the held change stays held
    release(co)
    sync(co, server)
    assert server.text("para.tex") == both and read(co.root, "para.tex") == both


def test_held_new_deleted_and_renamed_files(make, server):
    co = make()
    for name in ("a.tex", "b.tex", "c.tex"):
        write(co.root, name, f"{name}\n")
    sync(co, server)
    c_id = server.files()["c.tex"].id
    hold(co)
    write(co.root, "new.tex", "new\n")
    (co.root / "b.tex").unlink()
    os.rename(co.root / "c.tex", co.root / "d.tex")
    report = sync(co, server)
    assert report.empty()
    assert sorted(server.files()) == ["a.tex", "b.tex", "c.tex"]
    assert not (co.root / "b.tex").exists() and not (co.root / "c.tex").exists()
    assert co.held_paths == {"new.tex", "b.tex", "c.tex", "d.tex"}
    release(co)
    report = sync(co, server)
    assert report.added == ["new.tex"] and report.deleted == ["b.tex"] and report.renamed == [["c.tex", "d.tex"]]
    assert sorted(server.files()) == ["a.tex", "d.tex", "new.tex"]
    assert server.files()["d.tex"].id == c_id


def test_remote_delete_of_a_held_file_keeps_it(make, server):
    co = make()
    write(co.root, "a.tex", "first\n")
    sync(co, server)
    hold(co)
    write(co.root, "a.tex", "first, and more\n")
    y.remove(server.doc, server.files()["a.tex"].id)
    report = sync(co, server)
    assert report.trashed == [] and read(co.root, "a.tex") == "first, and more\n"
    assert "a.tex" not in server.files() and co.held_paths == {"a.tex"}
    release(co)
    assert sync(co, server).added == ["a.tex"]
    assert server.text("a.tex") == "first, and more\n"


def test_remote_delete_of_an_untouched_file_while_held(make, server):
    co = make()
    write(co.root, "a.tex", "a\n")
    sync(co, server)
    hold(co)
    y.remove(server.doc, server.files()["a.tex"].id)
    assert sync(co, server).trashed == ["a.tex"]


def test_remote_rename_moves_a_held_file(make, server):
    co = make()
    write(co.root, "old.tex", "one two three\n")
    sync(co, server)
    fid = server.files()["old.tex"].id
    hold(co)
    write(co.root, "old.tex", "one 2 three\n")
    y.rename(server.doc, fid, "sections/new.tex")
    report = sync(co, server)
    assert report.kept == ["sections/new.tex"]
    assert not (co.root / "old.tex").exists()
    assert read(co.root, "sections/new.tex") == "one 2 three\n"
    assert server.text("sections/new.tex") == "one two three\n"
    assert co.held_paths == {"sections/new.tex"}
    release(co)
    sync(co, server)
    assert server.text("sections/new.tex") == "one 2 three\n" and server.files()["sections/new.tex"].id == fid


def test_held_blob_change_stays_on_disk(make, server):
    co = make()
    write(co.root, "a.png", PNG)
    sync(co, server)
    uploads = server.blobs.uploads
    hold(co)
    write(co.root, "a.png", PNG + b"mine")
    sync(co, server)
    assert server.blobs.uploads == uploads and server.blobs.data[server.files()["a.png"].sha] == PNG
    server.put_blob("a.png", PNG + b"theirs")
    sync(co, server)
    assert (co.root / "a.png").read_bytes() == PNG + b"mine"
    release(co)
    sync(co, server)
    assert server.blobs.data[server.files()["a.png"].sha] == PNG + b"mine"


def test_restart_while_held_keeps_holding(make, server):
    co = make()
    write(co.root, "main.tex", "We measure energy.\n")
    sync(co, server)
    hold(co)
    write(co.root, "main.tex", "We carefully measure energy.\n")
    sync(co, server)
    y.put_text(server.doc, "main.tex", "We measure energy on GPUs.\n")
    restarted = make()
    sync(restarted, server)
    assert restarted.held and restarted.held_paths == {"main.tex"}
    assert server.text("main.tex") == "We measure energy on GPUs.\n"
    assert read(restarted.root, "main.tex") == "We carefully measure energy on GPUs.\n"
    again = make()  # and once more, now with a merged held file in the index
    sync(again, server)
    assert again.held_paths == {"main.tex"} and server.text("main.tex") == "We measure energy on GPUs.\n"
    release(again)
    sync(again, server)
    assert server.text("main.tex") == "We carefully measure energy on GPUs.\n"


def test_held_new_file_meets_a_remote_one_of_the_same_name(make, server):
    co = make()
    write(co.root, "main.tex", "x")
    sync(co, server)
    hold(co)
    write(co.root, "notes.tex", "ours\n")
    y.add_text(server.doc, "notes.tex", "theirs\n", file_id="0" * 32)
    report = sync(co, server)
    assert read(co.root, "notes.tex") == "ours\n" and any("notes.tex" in w for w in report.warnings)
    assert co.held_paths == {"notes.tex"}
    release(co)
    sync(co, server)
    files = server.files()
    ours = next(e for e in files.values() if e.text == "ours\n")
    assert files["notes.tex"].text == "theirs\n" and ours.path == "notes.tex"  # the lower id keeps the name
    conflict = y.conflict_path("notes.tex", ours.id)
    assert read(co.root, "notes.tex") == "theirs\n" and read(co.root, conflict) == "ours\n"
