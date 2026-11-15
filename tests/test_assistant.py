"""The new-paper assistant (app/assistant.py, docs/DESIGN.md §21).

Claude is never asked here. The route's tests replace ``ask_claude``, and the
tests of ``ask_claude`` itself replace the SDK's client with one that plays
back messages in the shapes Anthropic documents.
"""

import json
from types import SimpleNamespace

import pytest
from helpers import needs_db, running_app, second_client, sign_in

CFP = "https://conf.example.org/2027/cfp"

MAIN_TEX = r"""\documentclass[conference]{IEEEtran}
\begin{document}
\title{Untitled}
\maketitle
\section{Introduction}
\bibliographystyle{IEEEtran}
\bibliography{refs}
\end{document}"""


def _answer(**changes) -> dict:
    """An answer in assistant.SCHEMA, the way Claude gives it."""
    answer = {
        "found": True,
        "problem": "",
        "conference": "ICSE",
        "conference_name": "49th IEEE/ACM International Conference\non Software Engineering",
        "year": 2027,
        "track": "Research Track",
        "cfp_url": "https://conf.example.org/2027/research-track",
        "conference_url": "https://conf.example.org/2027",
        "submission_url": "https://icse2027.hotcrp.example.org",
        "location": "Dublin, Ireland",
        "publisher": "IEEE",
        "page_limit": "10 pages + 2 pages of references",
        "template": "IEEEtran conference",
        "review_model": "double-blind",
        "notes": "- Double-blind: no author names\n- Data availability statement required",
        "dates": [
            {"kind": "notification", "label": "", "date": "2026-10-20", "time": "", "tz": ""},
            {"kind": "submission", "label": "", "date": "2026-07-18", "time": "23:59", "tz": "AoE"},
            {"kind": "abstract", "label": "Abstract registration", "date": "2026-07-11", "time": "", "tz": "aoe"},
            {"kind": "other", "label": "Artifact submission", "date": "2026-11-05", "time": "17:00", "tz": "Europe/Dublin"},
            {"kind": "conference_start", "label": "", "date": "2027-04-25", "time": "", "tz": "Europe/Dublin"},
        ],
        "main_tex": MAIN_TEX,
        "author_kit_url": "https://www.ieee.example.org/conference-template.zip",
        "remarks": ["The camera-ready date is TBA."],
    }
    answer.update(changes)
    return answer


@pytest.fixture
def asked(monkeypatch):
    """Switch the assistant on with a Claude that answers ``asked.answer``.
    ``asked.calls`` are the ``(url, hint)`` it was asked with."""
    from app import assistant
    from app.config import get_settings

    state = SimpleNamespace(
        answer=_answer(), pages=[CFP, "https://conf.example.org/2027/dates", CFP], calls=[], failure=None
    )

    def ask(url: str, hint: str) -> tuple[dict, list[str]]:
        state.calls.append((url, hint))
        if state.failure:
            raise state.failure
        return state.answer, state.pages

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(assistant, "ask_claude", ask)
    return state


@needs_db
def test_off_without_a_key(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    with running_app() as client:
        assert client.get("/api/assistant").status_code == 401
        sign_in(client)
        assert client.get("/api/assistant").json() == {"enabled": False}
        r = client.post("/api/assistant/new-paper", json={"url": CFP})
        assert r.status_code == 503 and "not set up" in r.json()["detail"]


@needs_db
def test_the_answer_fills_a_new_paper(asked):
    with running_app() as client:
        sign_in(client)
        assert client.get("/api/assistant").json() == {"enabled": True}
        r = client.post("/api/assistant/new-paper", json={"url": f"  {CFP} ", "hint": " Research   track "})
        assert r.status_code == 200, r.text
        out = r.json()
        assert asked.calls == [(CFP, "Research track")]
        assert out["fields"] == {
            "conference": "ICSE",
            "conference_name": "49th IEEE/ACM International Conference on Software Engineering",
            "year": 2027,
            "track": "Research Track",
            "cfp_url": "https://conf.example.org/2027/research-track",
            "conference_url": "https://conf.example.org/2027",
            "submission_url": "https://icse2027.hotcrp.example.org",
            "location": "Dublin, Ireland",
            "publisher": "IEEE",
            "page_limit": "10 pages + 2 pages of references",
            "template": "IEEEtran conference",
            "review_model": "double-blind",
            "notes": "- Double-blind: no author names\n- Data availability statement required",
        }
        # Soonest first, a day without a time as `date`, a default label left
        # out, the zone as meertex stores it and AoE when Claude gave none.
        assert out["dates"] == [
            {"kind": "abstract", "tz": "AoE", "date": "2026-07-11"},
            {"kind": "submission", "tz": "AoE", "due_at": "2026-07-18T23:59"},
            {"kind": "notification", "tz": "AoE", "date": "2026-10-20"},
            {"kind": "other", "tz": "Europe/Dublin", "due_at": "2026-11-05T17:00", "label": "Artifact submission"},
            {"kind": "conference_start", "tz": "Europe/Dublin", "date": "2027-04-25"},
        ]
        assert out["starter"] == {
            "main_tex": MAIN_TEX + "\n",
            "author_kit_url": "https://www.ieee.example.org/conference-template.zip",
        }
        assert out["remarks"] == ["The camera-ready date is TBA."]
        assert out["pages"] == [CFP, "https://conf.example.org/2027/dates"]

        # The answer is a body POST /api/projects takes as it is.
        made = client.post("/api/projects", json={"title": "Green LLMs", **out["fields"], "dates": out["dates"]})
        assert made.status_code == 201, made.text
        project = made.json()
        assert project["name"] == "2027-icse-green-llms"
        assert [d["kind"] for d in project["dates"]] == ["abstract", "submission", "notification", "other", "conference_start"]
        submission = project["dates"][1]
        assert (submission["due_at"], submission["date_only"]) == ("2026-07-19T11:59:00Z", False)
        assert project["dates"][3]["label"] == "Artifact submission"


@needs_db
def test_what_claude_says_is_cleaned(asked):
    asked.answer = _answer(
        conference="  ICSE ",
        conference_name="x" * 900,
        year=0,
        track="",
        cfp_url="",
        conference_url="javascript:alert(1)",
        submission_url="ftp://conf.example.org/submit",
        review_model="Double-Blind",
        publisher="",
        notes="",
        dates=[
            {"kind": "submission", "label": "", "date": "2026-07-18", "time": "", "tz": "AoE"},
            {"kind": "submission", "label": "", "date": "2026-07-18", "time": "", "tz": "AoE"},
            {"kind": "notification", "label": "", "date": "soon", "time": "", "tz": "AoE"},
            {"kind": "camera_ready", "label": "", "date": "2027-01-10", "time": "", "tz": "Mars/Olympus"},
            {"kind": "party", "label": "", "date": "2027-01-10", "time": "", "tz": "AoE"},
            {"kind": "other", "label": "", "date": "2026-08-01", "time": "", "tz": "UTC+2"},
            "not a date",
        ],
        main_tex="Use the ACM template.",
        author_kit_url="file:///etc/passwd",
        remarks=["", "  Check the track.  ", 7],
    )
    asked.pages = ["https://conf.example.org/2027/cfp", "data:text/html,x"]
    with running_app() as client:
        sign_in(client)
        out = client.post("/api/assistant/new-paper", json={"url": "conf.example.org/2027/cfp"}).json()
        assert asked.calls == [(CFP, "")]
        assert out["fields"] == {
            "conference": "ICSE",
            "conference_name": "x" * 500,
            "location": "Dublin, Ireland",
            "page_limit": "10 pages + 2 pages of references",
            "template": "IEEEtran conference",
            "review_model": "double-blind",
            # Claude named no call for papers, so it is the address that was asked.
            "cfp_url": CFP,
        }
        assert out["dates"] == [
            {"kind": "submission", "tz": "AoE", "date": "2026-07-18"},
            {"kind": "other", "tz": "UTC+2", "date": "2026-08-01", "label": "Other deadline"},
        ]
        assert out["starter"] == {"main_tex": None, "author_kit_url": None}
        assert out["remarks"][0] == "Check the track."
        left_out = out["remarks"][1:]
        assert len(left_out) == 3 and all("left out" in r for r in left_out)
        assert "soon" in left_out[0] and "Mars/Olympus" in left_out[1] and "party" in left_out[2]
        assert out["pages"] == [CFP]


@needs_db
def test_no_call_for_papers_and_failures(asked):
    from app import assistant

    with running_app() as client:
        sign_in(client)
        asked.answer = _answer(found=False, problem="The page is a hotel booking site.")
        r = client.post("/api/assistant/new-paper", json={"url": CFP})
        assert r.status_code == 422
        assert r.json()["detail"] == "The assistant could not use this address. The page is a hotel booking site."

        empty = {key: "" for key in assistant.SCHEMA["properties"]}
        asked.answer = {**empty, "found": True, "year": 0, "dates": [], "remarks": []}
        r = client.post("/api/assistant/new-paper", json={"url": CFP})
        assert r.status_code == 422 and "neither a venue nor dates" in r.json()["detail"]

        asked.failure = assistant.Failed("Claude could not be reached.")
        r = client.post("/api/assistant/new-paper", json={"url": CFP})
        assert r.status_code == 502
        assert r.json()["detail"] == "The assistant could not read the call for papers. Claude could not be reached."


@needs_db
def test_addresses_claude_is_not_asked_about(asked):
    with running_app() as client:
        sign_in(client)
        for url in ("ftp://conf.example.org/cfp", "https://localhost/cfp", "nonsense", "https://u:p@conf.example.org/",
                    "https://conf.example.org/" + "a" * 250, "http://[::1"):
            r = client.post("/api/assistant/new-paper", json={"url": url})
            assert r.status_code == 422, url
        assert client.post("/api/assistant/new-paper", json={"url": CFP, "hint": "x" * 301}).status_code == 422
        assert asked.calls == []


@needs_db
def test_limits_per_account_and_for_the_server(asked, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_daily_per_account", 2)
    monkeypatch.setattr(settings, "assistant_daily_total", 3)
    with running_app() as client:
        sign_in(client)
        assert client.post("/api/assistant/new-paper", json={"url": CFP}).status_code == 200
        assert client.post("/api/assistant/new-paper", json={"url": CFP}).status_code == 200
        r = client.post("/api/assistant/new-paper", json={"url": CFP})
        assert r.status_code == 429 and "2 times within a day" in r.json()["detail"]
        # Claude is not asked for an account at its limit.
        assert len(asked.calls) == 2
        with second_client("arne@example.com") as other:
            assert other.post("/api/assistant/new-paper", json={"url": CFP}).status_code == 200
            r = other.post("/api/assistant/new-paper", json={"url": CFP})
            assert r.status_code == 429 and "as many requests today" in r.json()["detail"]
        assert len(asked.calls) == 3

        # A limit of 0 turns the assistant off.
        monkeypatch.setattr(settings, "assistant_daily_total", 0)
        assert client.get("/api/assistant").json() == {"enabled": False}
        assert client.post("/api/assistant/new-paper", json={"url": CFP}).status_code == 503


@needs_db
def test_busy_when_every_slot_is_taken(asked):
    from app.routers import assistant as routes

    with running_app() as client:
        sign_in(client)
        taken = [routes._slots.acquire(blocking=False) for _ in range(routes.MAX_AT_ONCE)]
        try:
            assert all(taken)
            r = client.post("/api/assistant/new-paper", json={"url": CFP})
            assert r.status_code == 503 and "busy" in r.json()["detail"]
        finally:
            for _ in taken:
                routes._slots.release()
        assert client.post("/api/assistant/new-paper", json={"url": CFP}).status_code == 200
        assert len(asked.calls) == 1


# --- The conversation with Claude --------------------------------------------------


def _message(stop_reason: str, content: list[dict]):
    from anthropic.types.beta import BetaMessage

    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    })


def _fetch(n: int, url: str) -> list[dict]:
    """A fetch and its result, as Anthropic's web fetch tool returns them."""
    return [
        {"type": "server_tool_use", "id": f"srvtoolu_{n}", "name": "web_fetch", "input": {"url": url}},
        {"type": "web_fetch_tool_result", "tool_use_id": f"srvtoolu_{n}", "content": {
            "type": "web_fetch_result", "url": url, "retrieved_at": "2026-11-15T10:30:00Z",
            "content": {"type": "document", "title": "Call for papers",
                        "source": {"type": "text", "media_type": "text/plain", "data": "Submit by 18 July."}}}},
    ]


def _failed_fetch(n: int, url: str) -> list[dict]:
    return [
        {"type": "server_tool_use", "id": f"srvtoolu_{n}", "name": "web_fetch", "input": {"url": url}},
        {"type": "web_fetch_tool_result", "tool_use_id": f"srvtoolu_{n}",
         "content": {"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"}},
    ]


@pytest.fixture
def claude(monkeypatch):
    """The SDK's client, replaced by one that plays ``claude.replies`` back
    and keeps what each request sent in ``claude.requests``."""
    import anthropic

    from app.config import get_settings

    class Stream:
        def __init__(self, reply):
            self.reply = reply

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    class Client:
        def __init__(self, **kwargs):
            Client.made.append(kwargs)
            self.beta = self.messages = self

        def stream(self, **kwargs):
            # A copy, because the conversation's list grows after the call.
            Client.requests.append({**kwargs, "messages": list(kwargs["messages"])})
            return Stream(Client.replies.pop(0))

        def close(self):
            pass

    Client.replies, Client.requests, Client.made = [], [], []
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", " test-key ")
    monkeypatch.setattr(get_settings(), "assistant_claude_model", "claude-sonnet-5-5")
    return Client


def test_one_conversation_reads_the_pages(claude):
    from app import assistant

    answer = _answer()
    claude.replies = [_message("end_turn", [
        {"type": "text", "text": "I will read the call."},
        *_fetch(1, CFP),
        *_failed_fetch(2, "https://conf.example.org/2027/broken"),
        *_fetch(3, "https://conf.example.org/2027/dates"),
        {"type": "text", "text": json.dumps(answer)},
    ])]
    got, pages = assistant.ask_claude(CFP, "Research track")
    assert got == answer
    assert pages == [CFP, "https://conf.example.org/2027/dates"]

    assert claude.made == [{"api_key": "test-key", "max_retries": 2, "timeout": 300.0}]
    (request,) = claude.requests
    assert request["model"] == "claude-sonnet-5-5"
    assert request["tools"] == [{"type": "web_fetch_20260318", "name": "web_fetch", "max_uses": 8,
                                 "max_content_tokens": 40_000}]
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": assistant.SCHEMA}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["system"] == assistant.SYSTEM
    # Web fetch only reads an address that a user message names.
    (message,) = request["messages"]
    assert message["role"] == "user"
    assert message["content"].startswith(f"Call for papers: {CFP}\nTrack or cycle: Research track\nToday is 20")


def test_a_paused_turn_is_sent_back_to_go_on(claude):
    from app import assistant

    paused = _message("pause_turn", _fetch(1, CFP))
    claude.replies = [paused, _message("end_turn", [{"type": "text", "text": json.dumps(_answer())}])]
    got, pages = assistant.ask_claude(CFP, "")
    assert got["conference"] == "ICSE" and pages == [CFP]
    first, second = claude.requests
    assert "Track or cycle: not named" in first["messages"][0]["content"]
    assert second["messages"] == [first["messages"][0], {"role": "assistant", "content": paused.content}]

    # It gives up when Anthropic keeps pausing.
    claude.replies = [paused] * (assistant.MAX_CONTINUES + 1)
    with pytest.raises(assistant.Failed, match="did not finish"):
        assistant.ask_claude(CFP, "")
    assert claude.replies == []


def test_answers_that_cannot_be_used(claude):
    import anthropic
    import httpx2  # what the SDK itself is built on

    from app import assistant

    def status_error(cls, code: int):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        return cls("no", response=httpx2.Response(code, request=request), body=None)

    cases = [
        (_message("refusal", []), "declined"),
        (_message("max_tokens", [{"type": "text", "text": '{"found": tr'}]), "cut off"),
        (_message("end_turn", [{"type": "text", "text": "Here are the dates."}]), "could not be read"),
        (_message("end_turn", [{"type": "text", "text": "[1, 2]"}]), "could not be read"),
        (_message("end_turn", []), "could not be read"),
        (status_error(anthropic.AuthenticationError, 401), "API key was refused"),
        (status_error(anthropic.NotFoundError, 404), "does not know the model claude-sonnet-5-5"),
        (status_error(anthropic.RateLimitError, 429), "rate limited"),
        (status_error(anthropic.BadRequestError, 400), "refused the request"),
        (status_error(anthropic.InternalServerError, 529), "answered 529"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
         "could not be reached"),
    ]
    for reply, words in cases:
        claude.replies = [reply]
        with pytest.raises(assistant.Failed, match=words):
            assistant.ask_claude(CFP, "")


def test_the_schema_fits_anthropics_structured_outputs():
    """Every object closed and every property required, which keeps the
    schema clear of the limit on optional properties."""
    from app import assistant
    from app.models import DATE_KINDS

    def walk(node: dict) -> None:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
            for child in node["properties"].values():
                walk(child)
        elif node.get("type") == "array":
            walk(node["items"])

    walk(assistant.SCHEMA)
    assert assistant.SCHEMA["properties"]["dates"]["items"]["properties"]["kind"]["enum"] == list(DATE_KINDS)


def test_the_cleaned_fields_fit_the_projects_own_limits():
    from app import assistant
    from app.project_schemas import ProjectCreate

    long = "x" * 100_000
    answer = _answer(**{name: long for name in assistant._LINES}, notes=long)
    out = assistant.suggest(CFP, ask=lambda url, hint: (answer, []))
    ProjectCreate.model_validate({**out["fields"], "dates": out["dates"]})
