"""Authentication: email magic links + sign-in codes + opaque session cookies.

meerpad's scheme (itself meerato's), carried over unchanged so the apps sign in
the same way: enter your email, get a link and a six-character code, either one
signs you in. The CLI uses the code, then asks for an API token.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from .config import get_settings
from .database import DB
from .models import (
    LoginCode,
    LoginToken,
    RateLimit,
    Session,
    User,
    utcnow,
)

settings = get_settings()
SESSION_COOKIE = "meertex_session"

# Digits and letters, minus the pairs that get confused when a code is read off one
# screen and typed into another: 0/O, 1/I/L. 31**6 ≈ 887 million combinations.
LOGIN_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
LOGIN_CODE_LENGTH = 6


def new_id() -> str:
    return str(uuid.uuid4())


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_token() -> str:
    """Secret for the token API. URL-safe, ~32 chars (fits VARCHAR(64))."""
    return secrets.token_urlsafe(24)


def get_or_create_user(db: DBSession, email: str) -> User:
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(id=new_id(), email=email)
        db.add(user)
        db.flush()
    return user


def get_user_by_api_token(db: DBSession, token: str | None) -> User | None:
    if not token:
        return None
    return db.query(User).filter(User.api_token == token).one_or_none()


def create_login_token(
    db: DBSession, email: str, redirect_to: str | None = None, expires: bool = True
) -> str:
    """A sign-in link's token. ``expires=False`` is for invites, whose mail
    may sit unread for days."""
    token = new_token()
    db.add(
        LoginToken(
            token=token,
            email=email.strip().lower(),
            expires_at=utcnow() + timedelta(minutes=settings.login_token_ttl_minutes) if expires else None,
            redirect_to=redirect_to,
        )
    )
    db.commit()
    return token


def consume_login_token(db: DBSession, token: str) -> tuple[User, str | None]:
    lt = db.query(LoginToken).filter(LoginToken.token == token).one_or_none()
    if lt is None or (lt.expires_at is not None and lt.expires_at < utcnow()):
        raise HTTPException(status_code=400, detail="Invalid or expired login link")
    user = get_or_create_user(db, lt.email)
    db.commit()
    return user, lt.redirect_to


def _normalize_login_code(code: str) -> str:
    """Uppercase and strip the spaces/dashes people add when copying a code by hand."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash_login_code(code: str) -> str:
    return hmac.new(
        settings.secret_key.encode(), _normalize_login_code(code).encode(), hashlib.sha256
    ).hexdigest()


def create_login_code(db: DBSession, email: str) -> str:
    """Issue a sign-in code, replacing any code previously issued to this email."""
    email = email.strip().lower()
    code = "".join(secrets.choice(LOGIN_CODE_ALPHABET) for _ in range(LOGIN_CODE_LENGTH))
    db.merge(
        LoginCode(
            email=email,
            code_hash=_hash_login_code(code),
            expires_at=utcnow() + timedelta(minutes=settings.login_token_ttl_minutes),
            attempts=0,
        )
    )
    db.commit()
    return code


def consume_login_code(db: DBSession, email: str, code: str) -> User:
    """Validate a sign-in code and burn it. Raises 400 on anything but a clean hit.

    SELECT ... FOR UPDATE serialises concurrent guesses so each one really costs
    one attempt (meerato measured the unlocked version leaking the cap under load).
    """
    email = email.strip().lower()
    invalid = HTTPException(status_code=400, detail="Invalid or expired code")

    row = db.query(LoginCode).filter(LoginCode.email == email).with_for_update().one_or_none()
    if row is None:
        raise invalid

    if row.expires_at < utcnow():
        db.delete(row)
        db.commit()
        raise invalid

    if not hmac.compare_digest(row.code_hash, _hash_login_code(code)):
        row.attempts += 1
        spent = row.attempts >= settings.login_code_max_attempts
        if spent:
            db.delete(row)
        db.commit()
        if spent:
            raise HTTPException(status_code=400, detail="Too many incorrect codes. Request a new one.")
        raise invalid

    db.delete(row)
    user = get_or_create_user(db, email)
    db.commit()
    return user


def _rate_hit(db: DBSession, key: str, limit: int, window_seconds: int) -> bool:
    """Count one request against a fixed window. True if it is within the limit."""
    now = utcnow()
    row = db.query(RateLimit).filter(RateLimit.key == key).with_for_update().one_or_none()

    if row is None:
        db.add(RateLimit(key=key, window_start=now, count=1))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return _rate_hit(db, key, limit, window_seconds)
        return 1 <= limit

    if (now - row.window_start).total_seconds() >= window_seconds:
        row.window_start = now
        row.count = 1
        db.commit()
        return 1 <= limit

    row.count += 1
    within = row.count <= limit
    db.commit()
    return within


def enforce_login_rate(db: DBSession, email: str) -> None:
    key = "login:" + email.strip().lower()
    window = settings.login_rate_window_minutes * 60
    if not _rate_hit(db, key, settings.login_rate_max, window):
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in requests for this email. Please wait a few minutes.",
        )


def enforce_verify_rate(db: DBSession, email: str) -> None:
    key = "verify:" + email.strip().lower()
    window = settings.login_rate_window_minutes * 60
    if not _rate_hit(db, key, settings.verify_rate_max, window):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please wait a few minutes and request a new code.",
        )


def create_session(db: DBSession, user: User) -> str:
    token = new_token()
    db.add(
        Session(
            token=token,
            user_id=user.id,
            expires_at=utcnow() + timedelta(minutes=settings.session_ttl_minutes),
        )
    )
    db.commit()
    return token


def _user_from_session(db: DBSession, token: str | None) -> User | None:
    if not token:
        return None
    sess = db.query(Session).filter(Session.token == token).one_or_none()
    if sess is None or sess.expires_at < utcnow():
        return None
    return db.get(User, sess.user_id)


SessionCookie = Annotated[str | None, Cookie()]


def get_current_user(db: DB, meertex_session: SessionCookie = None) -> User:
    user = _user_from_session(db, meertex_session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def get_optional_user(db: DB, meertex_session: SessionCookie = None) -> User | None:
    return _user_from_session(db, meertex_session)


def _bearer(authorization: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header."""
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip() or None
    return None


TokenQuery = Annotated[str | None, Query()]
AuthorizationHeader = Annotated[str | None, Header()]


def get_optional_user_or_token(
    db: DB,
    meertex_session: SessionCookie = None,
    token: TokenQuery = None,
    authorization: AuthorizationHeader = None,
) -> User | None:
    """Session cookie, or the account's API token as ``?token=`` or as
    ``Authorization: Bearer <token>`` (scripts, the CLI, agents). The header
    keeps the token out of URLs, which end up in proxy logs and shell history."""
    return (_user_from_session(db, meertex_session)
            or get_user_by_api_token(db, _bearer(authorization))
            or get_user_by_api_token(db, token))


def get_user_session_or_token(
    db: DB,
    meertex_session: SessionCookie = None,
    token: TokenQuery = None,
    authorization: AuthorizationHeader = None,
) -> User:
    """``get_optional_user_or_token``, or 401."""
    user = get_optional_user_or_token(db, meertex_session, token, authorization)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def delete_session(db: DBSession, token: str | None) -> None:
    if token:
        db.query(Session).filter(Session.token == token).delete()
        db.commit()


# Dependencies as types, for route signatures.
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
SessionOrTokenUser = Annotated[User, Depends(get_user_session_or_token)]
OptionalUserOrToken = Annotated[User | None, Depends(get_optional_user_or_token)]
