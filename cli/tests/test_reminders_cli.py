"""meertex reminders (cli/meertex/cmd_reminders.py) against a fake HTTP API."""

import json

import pytest
from meertex import cli, cmd_reminders
from test_cli import SERVER, FakeApi, run

PREVIEW = {
    "reminders": True,
    "reminder_days": [14, 3, 1, 0],
    "reminder_hour": 8,
    "timezone": "Europe/Berlin",
    "sending": True,
    "last_at": "2027-01-12T07:00:00Z",
    "next_at": "2027-01-13T07:00:00Z",
    "projects": [
        {
            "id": "p1", "name": "2027-icse-green-llm", "title": "Green LLM inference", "conference": "ICSE",
            "year": 2027, "planned": False, "role": "owner", "url": f"{SERVER}/p/2027-icse-green-llm",
            "items": [
                {"type": "question", "date_id": "d1", "kind": "abstract", "label": "Abstract registration",
                 "text": "The abstract registration deadline passed on 8 Jan 2027. Did you register the abstract?",
                 "answers": ["yes", "no", "moved"]},
                {"type": "date", "date_id": "d2", "kind": "submission", "label": "Paper submission",
                 "days_left": 2, "threshold": 3, "text": "Paper submission in 2 days, 15 Jan 2027 (AoE)."},
            ],
        },
        {
            "id": "p2", "name": "2027-ict4s", "title": "", "conference": "ICT4S", "year": 2027, "planned": True,
            "role": "owner", "url": f"{SERVER}/p/2027-ict4s",
            "items": [{"type": "date", "date_id": "d3", "kind": "submission", "label": "Paper submission",
                       "days_left": 14, "threshold": 14, "text": "Paper submission in 14 days, 27 Jan 2027 (AoE)."}],
        },
    ],
    "muted": [{"id": "p3", "name": "2026-old"}],
    "subject": "Paper submission in 2 days for 2027-icse-green-llm and 2 more",
    "body": "Hi,",
    "timeline_url": f"{SERVER}/timeline",
    "settings_url": f"{SERVER}/settings#reminders",
}


@pytest.fixture
def fake(monkeypatch):
    f = FakeApi()
    monkeypatch.setenv("MEERTEX_SERVER", SERVER)
    monkeypatch.setenv("MEERTEX_TOKEN", "tok")
    monkeypatch.setattr(cli, "api", lambda: f.api())
    return f


def test_days_text():
    assert cmd_reminders.days_text([14, 3, 1, 0]) == "14, 3 and 1 days before and on the day"
    assert cmd_reminders.days_text([1]) == "1 day before"
    assert cmd_reminders.days_text([0]) == "on the day"
    assert cmd_reminders.days_text([]) == "no days before a date"


def test_reminders_prints_the_preview(fake, capsys):
    fake.routes[("GET", "/api/reminders/preview")] = PREVIEW
    code, out, _err = run(capsys, "reminders")
    assert code == 0
    assert fake.calls == [("GET", "/api/reminders/preview", {}, b"")]
    assert "Reminder mails are on, 14, 3 and 1 days before and on the day, at 08:00 Europe/Berlin." in out
    assert "The next one is put together on 2027-01-13 at 08:00 Europe/Berlin" in out
    assert "2027-icse-green-llm  Green LLM inference  (ICSE 2027)" in out
    assert "  question  The abstract registration deadline passed on 8 Jan 2027." in out
    assert "  date      Paper submission in 2 days, 15 Jan 2027 (AoE)." in out
    assert "2027-ict4s  ICT4S 2027, still a plan" in out
    assert f"  {SERVER}/p/2027-ict4s" in out
    assert "Muted: 2026-old" in out


def test_reminders_json_and_nothing_to_say(fake, capsys):
    fake.routes[("GET", "/api/reminders/preview")] = PREVIEW
    code, out, _err = run(capsys, "reminders", "--json")
    assert code == 0 and json.loads(out) == PREVIEW

    fake.routes[("GET", "/api/reminders/preview")] = {
        **PREVIEW, "reminders": False, "next_at": None, "sending": False, "projects": [], "muted": [],
    }
    code, out, _err = run(capsys, "reminders")
    assert code == 0
    assert "Reminder mails are off." in out
    assert "REMINDERS_ENABLED is off" in out
    assert "Nothing to remind you of right now." in out
