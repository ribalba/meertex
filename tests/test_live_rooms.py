"""Room background work that is not about a connection: deadline snapshots."""

import time
import uuid
from datetime import UTC, timedelta

from helpers import needs_db, running_app
from test_live_helpers import make_project, make_user, wait_for

pytestmark = needs_db


def test_a_deadline_gets_a_snapshot_dated_with_it(monkeypatch):
    from app import live
    from app.database import SessionLocal
    from app.live import git, rooms
    from app.models import ProjectDate, utcnow

    monkeypatch.setattr(rooms, "DEADLINE_SCAN_SECONDS", 0.2)
    with running_app():
        owner_id, email, _token = make_user()
        pid, _name = make_project(owner_id)
        live.write_file(pid, "main.tex", b"handed in", email)
        due = (utcnow() + timedelta(seconds=2)).replace(microsecond=0)
        with SessionLocal() as db:
            db.add(ProjectDate(id=str(uuid.uuid4()), project_id=pid, kind="submission", due_at=due))
            db.commit()

        # No edit for a minute is needed: the deadline itself snapshots.
        stamp = str(int(due.replace(tzinfo=UTC).timestamp()))
        assert wait_for(lambda: git.run(pid, "log", "-1", "--format=%ct").decode().strip() == stamp, timeout=8)

        # git dates have whole seconds, so the next edit is a second later.
        while utcnow() < due + timedelta(seconds=1.1):
            time.sleep(0.1)
        live.write_file(pid, "main.tex", b"after the deadline", email)
        later = live.snapshot(pid)

        at_deadline = live.commit_at(pid, due)
        assert at_deadline not in (None, later)
        assert git.show(pid, at_deadline, "main.tex") == b"handed in"
        assert f"Edits by {email}" in git.run(pid, "log", "-1", "--format=%s", at_deadline).decode()
