"""Sign-in: meerpad's magic link + code flow, unchanged apart from the names."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import aliased

from .. import reminders, when
from ..config import get_settings
from ..database import DB
from ..emailer import send_login_link, send_new_user
from ..example import PEOPLE, build
from ..models import LoginToken, Membership, Project, User, utcnow
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


def _client(agent: str) -> str:
    """Where a sign-in happened, from its User-Agent. The CLI names itself
    (cli/meertex/api.py), the desktop app keeps Electron's default."""
    if agent.startswith("meertex-cli/"):
        return "from the CLI"
    if "Electron/" in agent:
        return "in the desktop app"
    return "in a browser"


def _first_sign_in(db: DB, user: User, request: Request, background: BackgroundTasks, way: str) -> None:
    """Keep the account's first sign-in, make its example paper (§19) and
    mail NEW_USER_MAIL_TO where the person comes from (§2, Accounts). The
    update wins only once, so two sign-ins at the same moment make one
    example and send one mail."""
    if user.first_sign_in_at is not None:
        return
    first = (
        db.query(User)
        .filter(User.id == user.id, User.first_sign_in_at.is_(None))
        .update({"first_sign_in_at": utcnow()}, synchronize_session=False)
    )
    db.commit()
    if not first:
        return
    if settings.example_paper:
        # In the request, so that the paper list shows it at once.
        build.create_for(db, user)
    if not settings.new_user_mail_to:
        return
    # Every membership but an owner's is an invite (routers/members.py).
    inviter = aliased(User)
    rows = (
        db.query(Membership, Project, inviter)
        .join(Project, Project.id == Membership.project_id)
        .outerjoin(inviter, inviter.id == Membership.added_by)
        .filter(Membership.user_id == user.id, Membership.role != "owner")
        .order_by(Membership.added_at)
        .all()
    )
    invites = [
        (
            (f"{by.name} <{by.email}>" if by.name else by.email) if by else "Somebody",
            project.title or project.name,
            m.role,
            when.day_text(m.added_at),
        )
        for m, project, by in rows
    ]
    signed_in = db.query(func.count(User.id)).filter(User.first_sign_in_at.is_not(None)).scalar()
    # The example's made-up coauthors never sign in, and they wait for nothing.
    waiting = (
        db.query(func.count(User.id))
        .filter(User.first_sign_in_at.is_(None), User.email.not_in(PEOPLE))
        .scalar()
    )
    agent = request.headers.get("user-agent", "")
    background.add_task(
        send_new_user, settings.new_user_mail_to, user.email, invites, f"{way} {_client(agent)}",
        request.client.host if request.client else "", agent, signed_in, waiting,
    )


@router.post("/login")
def request_login(payload: LoginRequest, db: DB) -> dict:
    """Email a magic sign-in link and a short sign-in code for the same account.
    The CLI (``client="cli"``) asks for the code, the browser takes either.

    The desktop app (electron/) sends ``client="desktop"``. Its link is a
    ``meertex://`` deep link that finishes sign-in inside the app window rather
    than in the user's browser. The code, typed into the window, always works."""
    enforce_login_rate(db, payload.email)
    token = create_login_token(db, payload.email, _safe_next(payload.next))
    code = create_login_code(db, payload.email)
    if payload.client == "desktop":
        link = f"meertex://login?token={token}"
    else:
        link = f"{settings.base_url}/api/auth/callback?token={token}"
    send_login_link(payload.email, link, code)
    return {"ok": True, "message": "Check your email for a sign-in link and code."}


@router.post("/verify-code")
def verify_login_code(
    payload: VerifyCodeRequest, response: Response, request: Request, background: BackgroundTasks, db: DB
) -> dict:
    enforce_verify_rate(db, payload.email)
    user = consume_login_code(db, payload.email, payload.code)
    _set_session_cookie(response, create_session(db, user))
    _first_sign_in(db, user, request, background, "with the sign-in code")
    return {"ok": True, "redirect": "/"}


@router.get("/callback")
def login_callback(token: str, request: Request, background: BackgroundTasks, db: DB) -> Response:
    """Consume a magic link, establish a session cookie, and redirect into the app."""
    user, redirect_to = consume_login_token(db, token)
    session_token = create_session(db, user)
    # Only an invite's link never expires (routers/members.py).
    invite = db.get(LoginToken, token).expires_at is None
    _first_sign_in(db, user, request, background, "with the invite link" if invite else "with a sign-in link")
    resp = RedirectResponse(url=_safe_next(redirect_to), status_code=302)
    _set_session_cookie(resp, session_token)
    return resp


@router.get("/me", response_model=UserOut)
def me(user: SessionOrTokenUser) -> UserOut:
    return user


@router.patch("/me", response_model=UserOut)
def update_me(payload: ProfileUpdate, db: DB, user: SessionOrTokenUser) -> UserOut:
    data = payload.model_dump(exclude_unset=True)
    if data.get("timezone") is not None:
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
    if data.get("mention_mails") is not None:
        user.mention_mails = data["mention_mails"]
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
