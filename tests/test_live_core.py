"""The live core (app/live): rooms, the update store, git snapshots and tags."""

import time
import uuid
from datetime import timedelta

from helpers import needs_db, running_app

pytestmark = needs_db


def _project() -> str:
    """A bare project row (the projects API is not needed for the core)."""
    from app.database import SessionLocal
    from app.models import Project, User

    with SessionLocal() as db:
        user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@example.com")
        db.add(user)
        db.flush()
        project = Project(id=str(uuid.uuid4()), owner_id=user.id, name=f"p-{uuid.uuid4().hex[:6]}")
        db.add(project)
        db.commit()
        return project.id


def test_init_edit_snapshot_and_reload():
    from app import live
    from app.live import git, rooms, ydoc

    with running_app():
        pid = _project()
        first = live.init_project(pid, "basic")
        assert {f["path"] for f in live.list_files(pid)} == {"main.tex", "refs.bib"}
        assert git.show(pid, first, "main.tex").startswith(b"\\documentclass")

        room = rooms.get(pid)
        with room.edit("didi@example.com") as doc:
            ydoc.put_text(doc, "sections/intro.tex", "Grüße aus Potsdam 😀\n")
        second = live.snapshot(pid)
        assert second != first
        assert git.show(pid, second, "sections/intro.tex").decode() == "Grüße aus Potsdam 😀\n"
        assert "didi@example.com" in git.run(pid, "log", "-1", "--format=%B").decode()

        # Nothing changed: no new commit.
        assert live.snapshot(pid) == second

        # The document survives losing the room (a restart).
        rooms.drop(pid)
        files = {f["path"]: f for f in live.list_files(pid)}
        assert files["sections/intro.tex"]["size"] == len("Grüße aus Potsdam 😀\n".encode())


def test_tags_resolve_and_commit_at():
    from app import live
    from app.live import rooms, ydoc
    from app.models import utcnow

    with running_app():
        pid = _project()
        live.init_project(pid)
        with rooms.get(pid).edit() as doc:
            ydoc.put_text(doc, "main.tex", "submitted text")
        at_deadline = live.snapshot(pid)
        deadline = utcnow()
        time.sleep(1.1)  # git dates have whole seconds
        with rooms.get(pid).edit() as doc:
            ydoc.put_text(doc, "main.tex", "edited after the deadline")
        later = live.snapshot(pid)

        assert live.commit_at(pid, deadline) == at_deadline
        assert live.commit_at(pid, deadline - timedelta(days=365)) is None
        live.create_tag(pid, "submission", at_deadline)
        assert live.resolve_commit(pid, "submission") == at_deadline
        assert live.resolve_commit(pid, "HEAD") == later
        assert live.resolve_commit(pid, "--help") is None
        assert live.resolve_commit(pid, "nope") is None
        assert [f["path"] for f in live.list_files(pid, "submission")] == ["main.tex"]
        assert live.list_files(pid, "submission")[0]["kind"] == "text"
        live.delete_tag(pid, "submission")
        assert live.resolve_commit(pid, "submission") is None


def test_blobs_fork_and_delete():
    from app import live
    from app.config import get_settings
    from app.live import git, rooms, ydoc

    with running_app():
        src = _project()
        live.init_project(src)
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        sha = live.store_blob(src, png)
        assert live.blob_path(src, sha).read_bytes() == png
        assert live.blob_path(src, "../../etc/passwd") is None
        with rooms.get(src).edit() as doc:
            ydoc.put_blob(doc, "figures/plot.png", sha, len(png))
            ydoc.put_text(doc, "main.tex", "hello")
        live.snapshot(src)
        assert live.list_files(src, "HEAD")[0]["kind"] == "blob"

        dst = _project()
        commit = live.fork_project(src, dst, "Resubmission of the ICSE paper")
        assert git.show(dst, commit, "figures/plot.png") == png
        assert git.run(dst, "log", "-1", "--format=%s").decode().strip() == "Resubmission of the ICSE paper"
        assert {f["path"] for f in live.list_files(dst)} == {"figures/plot.png", "main.tex"}

        live.delete_project(src)
        assert not (get_settings().projects_dir / src).exists()
        # The fork is untouched.
        assert live.blob_path(dst, sha) is not None


def test_paths_switching_between_file_and_folder():
    from app import live
    from app.live import git, rooms, ydoc

    with running_app():
        pid = _project()
        live.init_project(pid)
        with rooms.get(pid).edit() as doc:
            ydoc.put_text(doc, "figures", "a file called figures")
        live.snapshot(pid)
        with rooms.get(pid).edit() as doc:
            ydoc.remove(doc, ydoc.find(doc, "figures").id)
            ydoc.put_text(doc, "figures/data.csv", "a,b\n")
        head = live.snapshot(pid)
        assert git.show(pid, head, "figures/data.csv") == b"a,b\n"
