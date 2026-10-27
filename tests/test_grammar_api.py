"""/api/grammar over HTTP (DESIGN §17): each user's settings, ignoring words
and rules, the language list and the check, with the test database.

LanguageTool is the fake from tests/test_grammar_unit.py, through
checker.use_transport. GRAMMAR_URL is empty in the test app, so each test that
needs the checker switches it on with the ``lt`` fixture.
"""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from helpers import api_token, needs_db, running_app, second_client, sign_in
from test_grammar_unit import JAVA_LANGUAGES, LOCAL, Checker

from app.config import Settings
from app.grammar import checker

pytestmark = needs_db

DEFAULTS = {"available": False, "enabled": True, "language": "auto", "variants": [], "picky": False,
            "words": [], "disabled_rules": []}


@pytest.fixture
def client():
    with running_app() as c:
        sign_in(c)
        yield c


@pytest.fixture
def lt(monkeypatch):
    settings = Settings(_env_file=None, grammar_url=LOCAL)
    monkeypatch.setattr(checker, "get_settings", lambda: settings)
    fake = Checker()
    checker.use_transport(httpx.MockTransport(fake))
    yield fake
    checker.use_transport(None)


def stored(email: str = "didi@example.com"):
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        return db.query(User).filter(User.email == email).one().grammar


def put(client, body):
    return client.put("/api/grammar/config", json=body)


def ignore(client, body):
    return client.post("/api/grammar/ignore", json=body)


def check(client, text, **body):
    return client.post("/api/grammar/check", json={"text": text, **body})


# --- Settings ------------------------------------------------------------------------


def test_the_defaults_before_anything_is_saved(client):
    r = client.get("/api/grammar/config")
    assert r.status_code == 200
    assert r.json() == DEFAULTS
    assert stored() == {}


def test_available_follows_grammar_url(client, lt):
    assert client.get("/api/grammar/config").json() == {**DEFAULTS, "available": True}


def test_a_saved_setting_round_trips(client):
    r = put(client, {"language": "de-DE", "variants": ["de-AT", "en-GB", "de-AT"], "picky": True})
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg == {**DEFAULTS, "language": "de-DE", "variants": ["de-AT", "en-GB"], "picky": True}
    assert client.get("/api/grammar/config").json() == cfg

    # A PUT is a patch. What is left out keeps its value.
    cfg = put(client, {"enabled": False}).json()
    assert cfg["enabled"] is False and cfg["language"] == "de-DE" and cfg["picky"] is True
    assert stored()["enabled"] is False


def test_words_saved_in_bulk_are_cleaned_up(client):
    r = put(client, {"words": [" zebra ", "Kubernetes", "kubernetes", "apple"]})
    assert r.json()["words"] == ["apple", "Kubernetes", "zebra"]


@pytest.mark.parametrize("body", [
    {"language": "english"},
    {"language": "de_DE"},
    {"language": "x" * 41},
    {"language": 5},
    {"variants": ["auto"]},
    {"variants": [f"de-X{i:02d}" for i in range(13)]},
    {"variants": "de-AT"},
    {"disabled_rules": ["has space"]},
    {"disabled_rules": ["two,rules"]},
    {"words": ["two\nlines"]},
    {"words": "Kubernetes"},
    {"words": [7]},
    {"enabled": "yes"},
    {"enabled": 1},
    {"picky": "true"},
    {"enabled": None},
    {"language": None},
    ["enabled"],
])
def test_invalid_settings_are_refused_and_change_nothing(client, body):
    r = put(client, body)
    assert r.status_code == 422, (body, r.text)
    assert stored() == {}
    assert client.get("/api/grammar/config").json() == DEFAULTS


def test_settings_need_a_signed_in_user():
    with running_app() as c:
        assert c.get("/api/grammar/config").status_code == 401
        assert put(c, {"enabled": False}).status_code == 401
        assert ignore(c, {"word": "x"}).status_code == 401
        assert check(c, "Teh").status_code == 401
        assert c.get("/api/grammar/languages").status_code == 401


def test_the_api_token_works_too(client, lt):
    """Like every route (DESIGN §11), for scripts and agents."""
    from test_live_helpers import bare_client

    token = api_token(client)
    with bare_client() as c:
        headers = {"Authorization": f"Bearer {token}"}
        assert c.put("/api/grammar/config", json={"picky": True}, headers=headers).json()["picky"] is True
        r = c.post("/api/grammar/check", json={"text": "Teh cat."}, headers=headers)
        assert r.status_code == 200 and r.json()["matches"][0]["offset"] == 0


def test_each_user_has_their_own_settings(client):
    assert put(client, {"language": "de-DE", "words": ["Meertex"]}).status_code == 200
    with second_client("other@example.com") as other:
        assert other.get("/api/grammar/config").json() == DEFAULTS
        assert ignore(other, {"word": "Kubectl"}).status_code == 200
    assert client.get("/api/grammar/config").json()["words"] == ["Meertex"]
    assert stored("other@example.com")["words"] == ["Kubectl"]


# --- Ignoring -------------------------------------------------------------------------


def test_ignore_adds_words_once_ignoring_case(client):
    r = ignore(client, {"word": "Kubernetes"})
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["words"] == ["Kubernetes"]
    # The same word in another case is already there. Nothing changes, and it
    # is not an error either.
    r = ignore(client, {"word": "kubernetes"})
    assert (r.status_code, r.json()) == (200, cfg)
    assert ignore(client, {"word": "  Meertex "}).json()["words"] == ["Kubernetes", "Meertex"]
    assert ignore(client, {"word": "apple"}).json()["words"] == ["apple", "Kubernetes", "Meertex"]
    assert stored()["words"] == ["apple", "Kubernetes", "Meertex"]


def test_ignore_switches_rules_off_once(client):
    assert ignore(client, {"rule": "EN_A_VS_AN"}).json()["disabled_rules"] == ["EN_A_VS_AN"]
    cfg = ignore(client, {"rule": "EN_A_VS_AN", "word": "Teh"}).json()
    assert cfg["disabled_rules"] == ["EN_A_VS_AN"] and cfg["words"] == ["Teh"]


@pytest.mark.parametrize("body", [{}, {"word": ""}, {"word": "   "}, {"word": "two\nlines"}, {"word": None},
                                  {"word": 3}, {"rule": "a b"}, {"rule": ""}, {"rule": None}, "teh"])
def test_ignore_refuses_what_it_cannot_store(client, body):
    r = ignore(client, body)
    assert r.status_code == 422, (body, r.text)
    assert stored() == {}


def test_concurrent_ignores_all_land(client):
    """Two clicks at once must not cost one of the two words. Each is a
    read-modify-write of the user row, which is why it is read FOR UPDATE.
    Driven through the router's own write, each in a session of its own, as
    two requests would be."""
    from app.database import SessionLocal
    from app.models import User
    from app.routers.grammar import _save

    words = [f"word{i:02d}" for i in range(16)]

    def add(word):
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == "didi@example.com").one()
            return _save(db, user, lambda cur: checker.merge(cur, {"words": [*cur["words"], word]}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, words))
    assert client.get("/api/grammar/config").json()["words"] == words


# --- Languages ------------------------------------------------------------------------


def test_languages_need_a_checker(client):
    r = client.get("/api/grammar/languages")
    assert r.status_code == 409
    assert r.json()["detail"] == checker.NOT_CONFIGURED


def test_languages_are_listed(client, lt):
    lt.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES)
    r = client.get("/api/grammar/languages")
    assert r.status_code == 200
    assert [row["code"] for row in r.json()["languages"]] == ["ar", "en-US", "de-DE", "de-DE-x-simple-language"]


# --- Checking -------------------------------------------------------------------------


def test_a_check_without_a_checker_is_refused(client):
    r = check(client, "Teh cat.")
    assert r.status_code == 409
    assert r.json()["detail"] == checker.NOT_CONFIGURED


def test_a_check_when_turned_off_is_refused(client, lt):
    assert put(client, {"enabled": False}).status_code == 200
    r = check(client, "Teh cat.")
    assert r.status_code == 409
    assert r.json()["detail"] == "Spelling and grammar checking is turned off in your settings."
    assert lt.requests == []


def test_a_check(client, lt):
    text = "\\section{Intro}\n\nWe recieve $x$ and Teh."
    r = check(client, text, kind="latex", language="en-US", focus=0)
    assert r.status_code == 200, r.text
    body = r.json()
    # The setting is still auto, the editor sent the code detected before.
    assert body["language"] == {"code": "en-US", "name": "English (US)", "auto": True}
    assert [(m["offset"], m["length"]) for m in body["matches"]] == [(text.index("recieve"), 7),
                                                                     (text.index("Teh"), 3)]
    assert body["matches"][0]["type"] == "spelling"
    assert body["pending"] == [] and body["complete"] is True


def test_the_stored_language_and_dictionary_are_used(client, lt):
    put(client, {"language": "de-DE", "words": ["teh"]})
    body = check(client, "Teh und recieve.").json()
    assert lt.form()["language"] == "de-DE"
    assert [m["offset"] for m in body["matches"]] == [8]


@pytest.mark.parametrize("body", [
    {"text": "x" * (checker.MAX_TEXT_CHARS + 1)},
    {"text": 5},
    {},
    {"text": "Teh", "kind": "markdown"},
    {"text": "Teh", "kind": None},
    {"text": "Teh", "language": "english"},
    {"text": "Teh", "focus": -1},
    {"text": "Teh", "focus": "3"},
    {"text": "Teh", "focus": 1.5},
])
def test_a_check_with_bad_input_is_a_422(client, lt, body):
    r = client.post("/api/grammar/check", json=body)
    assert r.status_code == 422, (body, r.text)
    assert lt.requests == []


def test_the_longest_file_is_checked(client, lt):
    r = check(client, "Teh. " + "x" * (checker.MAX_TEXT_CHARS - 5))
    assert r.status_code == 200, r.text


def test_a_second_check_of_the_same_user_at_once_is_a_429(client, lt):
    me = client.get("/api/auth/me").json()
    with checker.one_check(me["id"]):
        r = check(client, "Teh cat.")
        assert r.status_code == 429
        assert r.json()["detail"] == checker.ALREADY_CHECKING
        # Somebody else is not held up by it.
        with second_client("other@example.com") as other:
            assert check(other, "Teh cat.").status_code == 200
    assert check(client, "Teh cat.").status_code == 200


@pytest.mark.parametrize("response, status", [
    (httpx.Response(400, text="Error: 'xx' is not a language code known to LanguageTool."), 400),
    (httpx.Response(500, text="boom"), 502),
])
def test_checker_failures_have_their_status(client, lt, response, status):
    lt.handler = lambda r: response
    r = check(client, "Teh cat.")
    assert r.status_code == status
    assert "Teh" not in r.json()["detail"]


def test_an_unreachable_checker_is_a_503(client, lt):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    lt.handler = down
    r = check(client, "Teh cat.")
    assert r.status_code == 503
    assert r.json()["detail"] == checker.UNREACHABLE


def test_users_share_the_cache_and_not_their_dictionaries(client, lt):
    text = "Teh cat and recieve."
    assert ignore(client, {"word": "Teh"}).status_code == 200
    mine = check(client, text).json()
    with second_client("other@example.com") as other:
        theirs = check(other, text, language="en-US").json()
    # One request to the checker for both, as the first check detected en-US
    # and the second asked for it.
    assert len(lt.requests) == 1
    assert [m["offset"] for m in mine["matches"]] == [text.index("recieve")]
    assert [m["offset"] for m in theirs["matches"]] == [0, text.index("recieve")]


def test_pending_paragraphs_come_back_on_the_next_check(client, lt, monkeypatch):
    monkeypatch.setattr(checker, "BUDGET_CHARS", 30)
    text = "First Teh paragraph here.\n\nSecond Teh paragraph here."
    body = check(client, text, language="en-US").json()
    assert body["complete"] is False
    assert body["pending"] == [[text.index("Second"), len(text)]]
    assert len(body["matches"]) == 1
    body = check(client, text, language="en-US").json()
    assert body["complete"] is True and len(body["matches"]) == 2
