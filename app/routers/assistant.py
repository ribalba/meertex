"""The new-paper assistant's routes (docs/DESIGN.md §21).

Asking Claude takes a minute or two. The route is a plain ``def``, so it runs
in a worker thread and holds no database connection while it waits.
"""

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import assistant
from ..database import DB
from ..security import SessionOrTokenUser, enforce_assistant_rate

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# Conversations with Claude at once, for the whole server. One more is told
# to come back, so the assistant never takes all the worker threads.
MAX_AT_ONCE = 4
_slots = threading.BoundedSemaphore(MAX_AT_ONCE)


class NewPaperIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    # The track or the submission cycle, when the venue has several.
    hint: str | None = Field(default=None, max_length=assistant.MAX_HINT)


@router.get("")
def status(user: SessionOrTokenUser) -> dict:
    """Whether this server has the assistant. The new-paper form asks."""
    return {"enabled": assistant.enabled()}


@router.post("/new-paper")
def new_paper(body: NewPaperIn, db: DB, user: SessionOrTokenUser) -> dict:
    """Read a call for papers and return what the new-paper form is filled
    with. Nothing is created."""
    if not assistant.enabled():
        raise HTTPException(status_code=503, detail="The assistant is not set up on this server.")
    try:
        url = assistant.check_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    hint = " ".join((body.hint or "").split())
    if not _slots.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="The assistant is busy with other papers. Try again in a minute.")
    try:
        # Counted before Claude is asked, because a request that fails on
        # the way costs money as well. The count commits, which hands the
        # request's database connection back for the wait.
        enforce_assistant_rate(db, user.id)
        return assistant.suggest(url, hint)
    except assistant.NotACall as e:
        raise HTTPException(status_code=422, detail=f"The assistant could not use this address. {e.args[0]}") from None
    except assistant.Failed as e:
        raise HTTPException(
            status_code=502, detail=f"The assistant could not read the call for papers. {e.args[0]}"
        ) from None
    finally:
        _slots.release()
