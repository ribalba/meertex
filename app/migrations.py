"""Incremental schema migrations, applied automatically on startup.

meerpad's mechanism, unchanged. ``Base.metadata.create_all`` (see
:func:`database.init_db`) only ever *creates missing tables*. It never touches a
table that already exists. This module closes that gap with a short, ordered list
of migrations, each run once per database and recorded in ``schema_migrations``.

Adding one: write a ``_verb_noun(conn) -> bool`` function that returns whether it
changed anything, then append it to :data:`MIGRATIONS` with the next id. Never
edit or reorder an id that has shipped.
"""

import logging
from collections.abc import Callable

from sqlalchemy import Connection, Engine, text

logger = logging.getLogger("uvicorn.error")

# Arbitrary but fixed: every process that migrates this database takes the same
# advisory lock, so processes booting together apply the list one at a time.
_LOCK_KEY = 8_314_207_032


def _users_timezone_nullable(conn: Connection) -> bool:
    """A new account has no zone until the web app sends the browser's. The
    accounts made before keep the one they have."""
    nullable = conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'timezone'"
        )
    ).scalar()
    if nullable != "NO":
        return False
    conn.execute(text("ALTER TABLE users ALTER COLUMN timezone DROP NOT NULL"))
    return True


MIGRATIONS: list[tuple[str, Callable[[Connection], bool]]] = [
    ("0001_users_timezone_nullable", _users_timezone_nullable),
]


def run_migrations(engine: Engine) -> None:
    """Apply every migration this database has not seen yet, in one transaction."""
    if engine.dialect.name != "postgresql":
        return

    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  id text PRIMARY KEY,"
                "  applied_at timestamptz NOT NULL DEFAULT now()"
                ")"
            )
        )
        applied = {row[0] for row in conn.execute(text("SELECT id FROM schema_migrations"))}
        for migration_id, migrate in MIGRATIONS:
            if migration_id in applied:
                continue
            changed = migrate(conn)
            conn.execute(text("INSERT INTO schema_migrations (id) VALUES (:id)"), {"id": migration_id})
            logger.info("migration %s %s", migration_id, "applied" if changed else "skipped (already current)")
