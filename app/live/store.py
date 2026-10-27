"""Where the live documents are kept between restarts: Yjs updates in Postgres
(docs/DESIGN.md §8).

A project's document is every row of ``doc_updates`` for it, applied in id
order. Applying an update twice is harmless in Yjs, which is what makes
compaction simple: the full state replaces the rows it already contains, and a
row written meanwhile stays and is applied on top.
"""

from pycrdt import Doc
from sqlalchemy import func, text

from ..database import SessionLocal
from ..models import DocUpdate

# Above this many rows a project's updates are folded into one.
COMPACT_ABOVE_ROWS = 200


def load(project_id: str) -> tuple[Doc, int]:
    """The project's document and how many rows it was built from."""
    doc = Doc()
    rows = 0
    with SessionLocal() as db:
        for (data,) in (
            db.query(DocUpdate.data).filter(DocUpdate.project_id == project_id).order_by(DocUpdate.id).yield_per(200)
        ):
            doc.apply_update(data)
            rows += 1
    return doc, rows


def append(project_id: str, data: bytes) -> None:
    with SessionLocal() as db:
        db.add(DocUpdate(project_id=project_id, data=data))
        db.commit()


def row_count(project_id: str) -> int:
    with SessionLocal() as db:
        return db.query(func.count(DocUpdate.id)).filter(DocUpdate.project_id == project_id).scalar() or 0


def compact(project_id: str, full_state: bytes) -> None:
    """Replace the project's rows with ``full_state``, the whole document as one
    update. Rows written after the state was taken are kept."""
    with SessionLocal() as db:
        top = db.query(func.max(DocUpdate.id)).filter(DocUpdate.project_id == project_id).scalar()
        if top is None:
            return
        db.execute(
            text("DELETE FROM doc_updates WHERE project_id = :p AND id <= :top"),
            {"p": project_id, "top": top},
        )
        db.add(DocUpdate(project_id=project_id, data=full_state))
        db.commit()
