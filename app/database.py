"""Engine, sessions and schema bootstrap (meerpad's, without the sync counter)."""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings
from .migrations import run_migrations

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# The request's session, as a type (keeps Depends() out of argument defaults).
DB = Annotated[Session, Depends(get_db)]


# --- Schema bootstrap ----------------------------------------------------------

# Columns added to existing tables after that table's first release. create_all()
# creates missing *tables* but never alters an existing one. All entries must be
# nullable (or defaulted) so adding them needs no backfill. (meerato's pattern.)
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "projects": {
        "compiler": "VARCHAR(20) NOT NULL DEFAULT 'pdflatex'",
        "planned": "BOOLEAN NOT NULL DEFAULT false",
        "writing_days": "INTEGER",
    },
    "versions": {"compile_id": "VARCHAR(36)"},
    "users": {
        "writing_days": "INTEGER NOT NULL DEFAULT 30",
        "reminders": "BOOLEAN NOT NULL DEFAULT true",
        "reminder_days": "JSONB NOT NULL DEFAULT '[14, 3, 1, 0]'",
        "reminder_hour": "INTEGER NOT NULL DEFAULT 8",
        "reminded_at": "TIMESTAMP WITHOUT TIME ZONE",
    },
    "memberships": {"reminders_muted": "BOOLEAN NOT NULL DEFAULT false"},
}


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        missing = {name: sql for name, sql in columns.items() if name not in present}
        if not missing:
            continue
        with engine.begin() as conn:
            for name, sql_type in missing.items():
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def init_db() -> None:
    from . import models  # noqa: F401  (registers the tables on Base)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    run_migrations(engine)
