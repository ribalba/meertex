"""Sign-in: meerpad's magic link + code flow, unchanged apart from the names."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse

from .. import reminders
from ..config import get_settings
from ..database import DB
from ..emailer import send_login_link
from ..naming import validate_pattern
from ..schemas import LoginRequest, ProfileUpdate, UserOut, VerifyCodeRequest
from ..security import (
    SESSION_COOKIE,
    CurrentUser,
    SessionCookie,
    SessionOrTokenUser,
    consume_login_code,
    consume_login_token,
    create_login_code,
    create_login_token,
    create_session,
    delete_session,
    enforce_login_rate,
    enforce_verify_rate,
    new_api_token,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


def _safe_next(path: str | None) -> str:
    """Only same-site paths: "/s/abc" yes, "//evil.com" and "https://…" no."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path[:500]


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https"),
    )


@router.post("/login")
def request_login(payload: LoginRequest, db: DB) -> dict:
    """Email a magic sign-in link and a short sign-in code for the same account.
    The CLI (``client="cli"``) asks for the code, the browser takes either."""
    enforce_login_rate(db, payload.email)
    token = create_login_token(db, payload.email, _safe_next(payload.next))
    code = create_login_code(db, payload.email)
    link = f"{settings.base_url}/api/auth/callback?token={token}"
    send_login_link(payload.email, link, code)
    return {"ok": True, "message": "Check your email for a sign-in link and code."}


@router.post("/verify-code")
def verify_login_code(payload: VerifyCodeRequest, response: Response, db: DB) -> dict:
    enforce_verify_rate(db, payload.email)
    user = consume_login_code(db, payload.email, payload.code)
    _set_session_cookie(response, create_session(db, user))
    return {"ok": True, "redirect": "/"}


@router.get("/callback")
def login_callback(token: str, db: DB) -> Response:
    """Consume a magic link, establish a session cookie, and redirect into the app."""
    user, redirect_to = consume_login_token(db, token)
    session_token = create_session(db, user)
    resp = RedirectResponse(url=_safe_next(redirect_to), status_code=302)
    _set_session_cookie(resp, session_token)
    return resp


@router.get("/me", response_model=UserOut)
def me(user: SessionOrTokenUser) -> UserOut:
    return user


@router.patch("/me", response_model=UserOut)
def update_me(payload: ProfileUpdate, db: DB, user: SessionOrTokenUser) -> UserOut:
    data = payload.model_dump(exclude_unset=True)
    if "timezone" in data:
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=422, detail="Unknown timezone")
        user.timezone = data["timezone"]
    if "name" in data:
        user.name = (data["name"] or "").strip() or None
    if data.get("naming_pattern") is not None:
        pattern = data["naming_pattern"].strip()
        problem = validate_pattern(pattern)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        user.naming_pattern = pattern
    if data.get("writing_days") is not None:
        user.writing_days = data["writing_days"]
    if data.get("reminders") is not None:
        user.reminders = data["reminders"]
    if data.get("reminder_days") is not None:
        try:
            user.reminder_days = reminders.clean_days(data["reminder_days"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data.get("reminder_hour") is not None:
        user.reminder_hour = data["reminder_hour"]
    db.commit()
    db.refresh(user)
    return user


@router.post("/api-token", response_model=UserOut)
def ensure_api_token(db: DB, user: CurrentUser) -> UserOut:
    """Create the user's API token if they don't have one yet."""
    if not user.api_token:
        user.api_token = new_api_token()
        db.commit()
        db.refresh(user)
    return user


@router.post("/api-token/rotate", response_model=UserOut)
def rotate_api_token(db: DB, user: CurrentUser) -> UserOut:
    """Replace the API token (invalidates the old one)."""
    user.api_token = new_api_token()
    db.commit()
    db.refresh(user)
    return user


@router.post("/logout")
def logout(response: Response, db: DB, meertex_session: SessionCookie = None) -> dict:
    delete_session(db, meertex_session)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
