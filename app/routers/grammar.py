"""Spelling and grammar while writing, the editor's checker and each user's
settings for it (docs/DESIGN.md §17).

The checking is done by a LanguageTool server this install runs itself (the
`languagetool` service in docker-compose), and every request to it goes out
from here, never from the browser. Partly because the checker is on the compose
network, where the browser cannot reach it, and mostly because the server is
where the privacy guard can be enforced (app/grammar/checker.py). The text goes
to GRAMMAR_URL or nowhere.

The settings are a JSON object on the user row, users.grammar: on or off, the
language (or auto), preferred variants for auto-detection, the picky level, the
personal dictionary, and the rules switched off. This router owns the column,
app/grammar/checker.py owns its shape and validation. meerail's routes, with a
row per user instead of one for the install.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ..database import DB
from ..grammar import checker
from ..models import User
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api/grammar", tags=["grammar"])

TURNED_OFF = "Spelling and grammar checking is turned off in your settings."

LanguageCode = Annotated[str, StringConstraints(max_length=checker.MAX_LANGUAGE_CHARS)]
Word = Annotated[str, StringConstraints(max_length=checker.MAX_WORD_CHARS)]
RuleId = Annotated[str, StringConstraints(max_length=checker.MAX_RULE_CHARS)]


def _refuse_null(value: Any, field: str) -> Any:
    # Optional means "omit it to leave it alone", which is not the same as
    # accepting null.
    if value is None:
        raise ValueError(f"{field} cannot be null. Omit it to leave it unchanged, or send a value")
    return value


def _http_error(exc: checker.GrammarError) -> HTTPException:
    """The status code for each way a check can fail. The detail is always the
    checker module's own sentence, which never contains any of the text."""
    if isinstance(exc, checker.NotConfigured):
        code = 409
    elif isinstance(exc, (checker.PublicHost, checker.Refused)):
        code = 400
    elif isinstance(exc, checker.AlreadyChecking):
        code = 429
    elif isinstance(exc, (checker.Unreachable, checker.Busy)):
        code = 503
    else:
        code = 502
    return HTTPException(status_code=code, detail=str(exc))


def _shape(config: dict) -> dict:
    return {"available": checker.available(), **config}


def _save(db: DB, user: User, change: Callable[[dict], dict]) -> dict:
    """Read the user's settings, apply `change`, store the result, in one
    read-modify-write.

    The row is read FOR UPDATE, because two of these can overlap. The editor's
    "Add to dictionary" and "Turn off this rule" are one click each, and the
    settings page may save at the same time. Without the lock, two clicks that
    land together each add their word to the list as they read it, and
    whichever commits second writes the first one's word back out.
    """
    # populate_existing makes what is read overwrite the copy the session
    # already holds (the signed-in user), so the value is the locked one.
    row = db.query(User).filter(User.id == user.id).with_for_update().populate_existing().one()
    updated = change(checker.parse_stored(row.grammar))
    # A new dict, never the old one changed. SQLAlchemy does not notice a JSON
    # value mutated in place (app/models.py).
    row.grammar = dict(updated)
    db.commit()
    return updated


# --- Settings ------------------------------------------------------------------------


@router.get("/config")
def get_config(user: SessionOrTokenUser) -> dict:
    """Your settings, plus whether this server has a checker at all. `available`
    false means GRAMMAR_URL is empty, and the editor keeps the browser's own
    spellchecker."""
    return _shape(checker.parse_stored(user.grammar))


class ConfigIn(BaseModel):
    """Any subset of the settings. What is left out keeps its stored value."""

    # Strict, so `"enabled": "yes"` or `1` is a 422 rather than a guess.
    model_config = ConfigDict(strict=True)

    enabled: bool | None = None
    language: LanguageCode | None = None
    variants: Annotated[list[LanguageCode], Field(max_length=checker.MAX_VARIANTS)] | None = None
    picky: bool | None = None
    words: Annotated[list[Word], Field(max_length=checker.MAX_WORDS)] | None = None
    disabled_rules: Annotated[list[RuleId], Field(max_length=checker.MAX_RULES)] | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _not_null(cls, value, info):
        # Defaults are not validated in pydantic v2, so this only ever sees a
        # value the caller actually sent.
        return _refuse_null(value, info.field_name)


@router.put("/config")
def put_config(payload: ConfigIn, db: DB, user: SessionOrTokenUser) -> dict:
    changes = payload.model_dump(exclude_unset=True)

    def change(current: dict) -> dict:
        try:
            return checker.merge(current, changes)
        except checker.InvalidSetting as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _shape(_save(db, user, change))


class IgnoreIn(BaseModel):
    """A word for the dictionary (the editor's "Add to dictionary"), a rule id
    to switch off ("Turn off this rule"), or both at once."""

    model_config = ConfigDict(strict=True)

    word: Word | None = None
    rule: RuleId | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _not_null(cls, value, info):
        return _refuse_null(value, info.field_name)

    @model_validator(mode="after")
    def _something(self):
        if self.word is None and self.rule is None:
            raise ValueError("Send a word to add to the dictionary, a rule to switch off, or both")
        return self


@router.post("/ignore")
def ignore(payload: IgnoreIn, db: DB, user: SessionOrTokenUser) -> dict:
    """Add one word or one rule to what is ignored. Idempotent. A word already
    in the dictionary under any capitalisation, or a rule already off, changes
    nothing and is not an error."""
    try:
        word = checker.validate_word(payload.word) if payload.word is not None else None
        rule = checker.validate_rule(payload.rule) if payload.rule is not None else None
    except checker.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def change(current: dict) -> dict:
        changes: dict = {}
        if word is not None:
            changes["words"] = [*current["words"], word]
        if rule is not None:
            changes["disabled_rules"] = [*current["disabled_rules"], rule]
        try:
            return checker.merge(current, changes)
        except checker.InvalidSetting as exc:
            # The entry itself was checked above, so what is left is a list
            # that is full, which is a refusal of the operation rather than bad input.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _shape(_save(db, user, change))


# --- The checker -----------------------------------------------------------------------


@router.get("/languages")
def get_languages(user: SessionOrTokenUser) -> dict:
    """What the configured checker can check, for the language picker."""
    try:
        return {"languages": checker.languages()}
    except checker.GrammarError as exc:
        raise _http_error(exc) from exc


class CheckIn(BaseModel):
    model_config = ConfigDict(strict=True)

    # The whole open file. Its length is checked in the route, with a sentence.
    text: str
    # "latex" for .tex files, "text" for plain paragraphs (.md, .txt).
    kind: Literal["latex", "text"] = "latex"
    # Overrides the stored language for this one check. Null means the stored
    # one. With auto stored, the editor sends the code detected last time.
    language: LanguageCode | None = None
    # A UTF-16 offset into `text`, usually the cursor. Its paragraph goes first.
    focus: Annotated[int, Field(ge=0)] | None = None


@router.post("/check")
def check(payload: CheckIn, db: DB, user: SessionOrTokenUser) -> dict:
    if len(payload.text) > checker.MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"That file is {len(payload.text)} characters long. "
                   f"Files up to {checker.MAX_TEXT_CHARS} are checked.")
    if not checker.available():
        raise HTTPException(status_code=409, detail=checker.NOT_CONFIGURED)
    config = checker.parse_stored(user.grammar)
    if not config["enabled"]:
        raise HTTPException(status_code=409, detail=TURNED_OFF)
    user_id = user.id
    # Hand the connection back before waiting on the checker. A check can take
    # seconds (the first one in a language, twenty at worst), and it runs on
    # every pause in typing. Nothing after this reads the database.
    db.rollback()
    try:
        with checker.one_check(user_id):
            return checker.check(payload.text, config, kind=payload.kind, language=payload.language,
                                 focus=payload.focus)
    except checker.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except checker.GrammarError as exc:
        raise _http_error(exc) from exc
