"""Profile pictures (docs/DESIGN.md §2, Accounts).

``PUT /api/auth/me/avatar`` takes the picture as the raw body and
``DELETE`` goes back to the initials. Both answer with the user. Pictures are
served by their content's name, so a browser keeps one until it changes.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import avatars
from ..config import get_settings
from ..database import DB
from ..schemas import UserOut
from ..security import SessionOrTokenUser
from .files import SAFE_HEADERS, RawBody

router = APIRouter(prefix="/api", tags=["avatars"])


@router.put("/auth/me/avatar", response_model=UserOut)
def set_avatar(db: DB, user: SessionOrTokenUser, body: RawBody) -> UserOut:
    limit = get_settings().max_avatar_bytes
    if len(body) > limit:
        raise HTTPException(status_code=413, detail=f"A profile picture can be at most {limit // 1024} KB")
    fmt = avatars.image_format(body)
    if fmt is None:
        raise HTTPException(status_code=422, detail="A profile picture is a PNG, JPEG, GIF or WebP image")
    old = user.avatar
    user.avatar = avatars.store(body, fmt)
    db.commit()
    if old != user.avatar:
        avatars.forget(db, old)
    db.refresh(user)
    return user


@router.delete("/auth/me/avatar", response_model=UserOut)
def remove_avatar(db: DB, user: SessionOrTokenUser) -> UserOut:
    old = user.avatar
    user.avatar = None
    db.commit()
    avatars.forget(db, old)
    db.refresh(user)
    return user


@router.get("/avatars/{name}")
def get_avatar(name: str, user: SessionOrTokenUser) -> FileResponse:
    stored = avatars.path(name)
    if stored is None:
        raise HTTPException(status_code=404, detail="There is no such picture")
    fmt = name.rsplit(".", 1)[1]
    headers = {**SAFE_HEADERS, "Cache-Control": "private, max-age=31536000, immutable"}
    return FileResponse(stored, media_type=avatars.MEDIA_TYPES[fmt], headers=headers)
