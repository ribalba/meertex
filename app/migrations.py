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


def _login_tokens_expires_nullable(conn: Connection) -> bool:
    """An invite's sign-in link never expires, so its token has no
    ``expires_at``. The sign-in links keep theirs."""
    nullable = conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'login_tokens' AND column_name = 'expires_at'"
        )
    ).scalar()
    if nullable != "NO":
        return False
    conn.execute(text("ALTER TABLE login_tokens ALTER COLUMN expires_at DROP NOT NULL"))
    return True


def _users_first_sign_in_backfill(conn: Connection) -> bool:
    """``first_sign_in_at`` came after the first accounts. Those that show they
    signed in get their creation time: a session, an API token, the zone the
    web app sends on its first load, or a project of their own. An invited
    person with none of it is still waiting, and their first sign-in mails
    NEW_USER_MAIL_TO like anybody else's."""
    result = conn.execute(
        text(
            "UPDATE users SET first_sign_in_at = created_at"
            " WHERE first_sign_in_at IS NULL AND ("
            "  api_token IS NOT NULL OR timezone IS NOT NULL"
            "  OR EXISTS (SELECT 1 FROM sessions s WHERE s.user_id = users.id)"
            "  OR EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = users.id AND m.role = 'owner'))"
        )
    )
    return result.rowcount > 0


MIGRATIONS: list[tuple[str, Callable[[Connection], bool]]] = [
    ("0001_users_timezone_nullable", _users_timezone_nullable),
    ("0002_login_tokens_expires_nullable", _login_tokens_expires_nullable),
    ("0003_users_first_sign_in_backfill", _users_first_sign_in_backfill),
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
