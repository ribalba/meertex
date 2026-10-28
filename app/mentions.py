"""@-mentions in comments (docs/DESIGN.md §13, Mentions).

A mention is ``@`` and a member's name or email, compared ignoring case. The
web app's comment box completes it from the members (app/static/js/mentions.js),
the CLI and agents type it. app/static/js/markdown.js marks mentions with the
same rules, so what is mailed is what shows.

- The ``@`` must not follow a letter, a digit or ``_``, so an email address in
  the text is not a mention.
- What follows the name must not be a letter, a digit or ``_`` either.
- Where several names fit at one ``@``, the longest wins ("@Anna Lena" over
  "@Anna"). Two members with the same name are both mentioned by it, which is
  why the web app completes such a name to the email.
- Code (`...` and fenced blocks) mentions nobody.
"""

import re
from collections.abc import Iterable

from sqlalchemy.orm import Session as DBSession

from .models import Membership, Project, User

_FENCED = re.compile(r"^```.*?(?:^```[^\n]*$|\Z)", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")


def _wordish(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def labels(user: User) -> list[str]:
    """What may follow the ``@`` for this person."""
    return [s for s in ((user.name or "").strip(), user.email.strip()) if s]


def find(text: str, people: Iterable[User]) -> list[User]:
    """The people that ``text`` mentions, in the order they first come."""
    text = _INLINE_CODE.sub(" ", _FENCED.sub(" ", text or ""))
    candidates = sorted(((label, u) for u in people for label in labels(u)), key=lambda c: -len(c[0]))
    found: dict[str, User] = {}
    at = text.find("@")
    while at >= 0:
        nxt = at + 1
        if at == 0 or not _wordish(text[at - 1]):
            matched = None
            for label, user in candidates:
                end = at + 1 + len(label)
                if matched is not None and len(label) < len(matched):
                    break
                if text[at + 1 : end].lower() == label.lower() and not (end < len(text) and _wordish(text[end])):
                    # Every person with the longest name that fits.
                    matched = label
                    found.setdefault(user.id, user)
            if matched is not None:
                nxt = at + 1 + len(matched)
        at = text.find("@", nxt)
    return list(found.values())


def members(db: DBSession, project: Project) -> list[User]:
    return (
        db.query(User)
        .join(Membership, Membership.user_id == User.id)
        .filter(Membership.project_id == project.id)
        .order_by(User.email)
        .all()
    )


def mentioned_ids(db: DBSession, project: Project, *texts: str) -> list[str]:
    """The ids of the project's members that ``texts`` mention, for storing
    with a comment or a review."""
    if not any("@" in (t or "") for t in texts):
        return []
    people = members(db, project)
    ids: dict[str, None] = {}
    for t in texts:
        for user in find(t, people):
            ids.setdefault(user.id)
    return list(ids)
