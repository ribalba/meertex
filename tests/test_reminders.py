"""Reminder mails before deadlines (docs/DESIGN.md §5, Reminders).

The loop is off in the test apps (tests/conftest.py). The tests call
app.reminders with a "now" of their own and catch the mails by replacing
app.emailer.send_email.
"""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db

EMAIL = "didi@example.com"


def at(value: str) -> datetime:
    """A frozen "now", naive UTC like every datetime inside the app."""
    return datetime.fromisoformat(value)


@pytest.fixture
def mails(monkeypatch):
    from app import emailer

    caught: list[tuple[str, str, str]] = []
    monkeypatch.setattr(emailer, "send_email", lambda to, subject, body: caught.append((to, subject, body)))
    return caught


def send(mails: list, now: datetime) -> list[tuple[str, str, str]]:
    """Run the loop's work once at ``now`` and return the mails it sent."""
    from app import reminders

    mails.clear()
    count = reminders.send_due(now)
    assert count == len(mails)
    return list(mails)


def digest(now: datetime, email: str = EMAIL, fresh: bool = False):
    from app import reminders
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        return reminders.build(db, user, now, fresh_only=fresh)


def items(now: datetime, email: str = EMAIL) -> list[tuple[str, str, str]]:
    """(paper, type, kind) of everything a mail would say, whatever was said before."""
    return [(p["name"], i["type"], i["kind"]) for p in digest(now, email).papers for i in p["items"]]


def paper(client, name: str, dates: list[dict], **body) -> dict:
    body = {"name": name, "title": f"Paper {name}", "conference": "ICSE", "year": 2027, **body}
    r = client.post("/api/projects", json={**body, "dates": dates})
    assert r.status_code == 201, r.text
    return r.json()


def date_of(p: dict, kind: str) -> dict:
    return next(d for d in p["dates"] if d["kind"] == kind)


def set_user(email: str = EMAIL, **fields) -> None:
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        for key, value in fields.items():
            setattr(user, key, value)
        db.commit()


SUBMISSION = {"kind": "submission", "due_at": "2027-02-01T12:00", "tz": "UTC"}


# --- What a mail says --------------------------------------------------------------


def test_a_date_reminds_once_per_threshold(mails):
    with running_app() as client:
        sign_in(client)
        paper(client, "p", [SUBMISSION])
        assert send(mails, at("2027-01-17T09:00")) == []  # 15 days before

        [(to, subject, body)] = send(mails, at("2027-01-18T09:00"))
        assert to == EMAIL
        assert subject == "Paper submission in 14 days for p"
        assert "Paper p (ICSE 2027)\n- Paper submission in 14 days, 1 Feb 2027 at 12:00 UTC.\nhttp://testserver/p/p" in body
        assert "http://testserver/timeline" in body

        # Nothing new between two thresholds.
        assert send(mails, at("2027-01-19T09:00")) == []
        assert send(mails, at("2027-01-28T09:00")) == []
        assert "Paper submission in 3 days" in send(mails, at("2027-01-29T09:00"))[0][2]
        assert send(mails, at("2027-01-30T09:00")) == []
        assert "Paper submission tomorrow, 1 Feb 2027" in send(mails, at("2027-01-31T09:00"))[0][2]
        [(_to, subject, body)] = send(mails, at("2027-02-01T09:00"))
        assert subject == "Paper submission today for p"
        assert "- Paper submission today, 1 Feb 2027 at 12:00 UTC." in body


def test_a_date_inside_several_thresholds_reminds_once():
    with running_app() as client:
        sign_in(client)
        paper(client, "late", [SUBMISSION])
        # Added two days before. 14 and 3 are both crossed, and one item says it.
        d = digest(at("2027-01-30T09:00"), fresh=True)
        [[item]] = [p["items"] for p in d.papers]
        assert (item["days_left"], item["threshold"], item["text"]) == (
            2, 3, "Paper submission in 2 days, 1 Feb 2027 at 12:00 UTC.")
        assert sorted(k[2] for k in d.keys) == [3, 14]


def test_only_dates_to_hand_something_in_remind():
    with running_app() as client:
        sign_in(client)
        paper(client, "all", [
            {"kind": "abstract", "due_at": "2027-02-01T10:00", "tz": "UTC"},
            SUBMISSION,
            {"kind": "notification", "due_at": "2027-02-01T13:00", "tz": "UTC"},
            {"kind": "conference_start", "due_at": "2027-02-01T14:00", "tz": "UTC"},
            {"kind": "other", "label": "Artifact", "due_at": "2027-02-01T15:00", "tz": "UTC"},
        ])
        assert [kind for _p, _t, kind in items(at("2027-01-31T09:00"))] == ["abstract", "submission", "other"]


def test_an_aoe_deadline_counts_in_the_venues_day(mails):
    from app import reminders

    with running_app() as client:
        sign_in(client)
        p = paper(client, "aoe", [{"kind": "submission", "date": "2027-01-15", "tz": "AoE"}])
        # 23:59:59 AoE on 15 Jan is already 16 Jan in UTC.
        assert date_of(p, "submission")["due_at"] == "2027-01-16T11:59:59Z"
        set_user(reminder_days=[1, 0])

        # 13:00 UTC on 14 Jan is 01:00 on 14 Jan AoE. That is tomorrow, not in 2 days.
        [(_to, _subject, body)] = send(mails, at("2027-01-14T13:00"))
        assert "- Paper submission tomorrow, 15 Jan 2027 (AoE)." in body
        # 13:00 UTC on 15 Jan is 01:00 on 15 Jan AoE. That is today, although UTC says 16 Jan.
        [(_to, subject, body)] = send(mails, at("2027-01-15T13:00"))
        assert subject == "Paper submission today for aoe"
        assert "- Paper submission today, 15 Jan 2027 (AoE)." in body

        from app.models import ProjectDate

        d = ProjectDate(due_at=at("2027-01-16T11:59:59"), tz="AoE", date_only=True)
        assert reminders.days_left(d, at("2027-01-15T11:00")) == 1  # 23:00 on 14 Jan AoE
        assert reminders.days_left(d, at("2027-01-15T12:00")) == 0  # midnight AoE


def test_the_mail_waits_for_the_users_hour_in_their_zone(mails):
    from app import reminders

    with running_app() as client:
        sign_in(client)
        r = client.patch("/api/auth/me", json={"timezone": "Europe/Berlin", "reminder_hour": 8})
        assert r.status_code == 200, r.text
        paper(client, "p", [SUBMISSION])

        # 07:30 in Berlin (UTC+1 in winter).
        assert send(mails, at("2027-01-29T06:30")) == []
        # 08:05 in Berlin.
        assert len(send(mails, at("2027-01-29T07:05"))) == 1
        # Once a day. Nothing later that day, and nothing after midnight UTC either.
        assert send(mails, at("2027-01-29T15:00")) == []
        assert send(mails, at("2027-01-29T22:30")) == []
        # 00:30 on 30 Jan in Berlin is a new day, but before the hour.
        user = _user()
        assert not reminders.is_due(user, at("2027-01-29T23:30"))
        assert reminders.next_at(user, at("2027-01-29T23:30")) == at("2027-01-30T07:00")
        assert reminders.is_due(user, at("2027-01-30T07:00"))
        assert "tomorrow" in send(mails, at("2027-01-31T07:00"))[0][2]


def _user(email: str = EMAIL):
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        db.expunge(user)
        return user


def test_nothing_is_sent_twice_also_not_after_a_restart(mails):
    with running_app() as client:
        sign_in(client)
        paper(client, "p", [SUBMISSION])
        now = at("2027-01-29T09:00")
        assert len(send(mails, now)) == 1
        assert send(mails, now) == []
        # A new process knows nothing but the database. Even with the day
        # forgotten, what was told is not told again.
        set_user(reminded_at=None)
        assert send(mails, now) == []


def test_a_moved_date_reminds_again(mails):
    with running_app() as client:
        sign_in(client)
        p = paper(client, "p", [SUBMISSION])
        assert "in 3 days" in send(mails, at("2027-01-29T09:00"))[0][2]
        r = client.patch(f"/api/projects/{p['id']}/dates/{date_of(p, 'submission')['id']}",
                         json={"due_at": "2027-02-08T12:00", "tz": "UTC"})
        assert r.status_code == 200, r.text
        [(_to, _subject, body)] = send(mails, at("2027-01-30T09:00"))
        assert "- Paper submission in 9 days, 8 Feb 2027 at 12:00 UTC." in body
        assert send(mails, at("2027-01-31T09:00")) == []


def test_a_muted_paper_stays_quiet(mails):
    with running_app() as client:
        sign_in(client)
        p = paper(client, "loud", [SUBMISSION])
        paper(client, "quiet", [SUBMISSION])
        r = client.put(f"/api/projects/{p['id']}/reminders", json={"muted": True})
        assert r.status_code == 200, r.text
        assert r.json()["reminders_muted"] is True
        listed = {s["name"]: s["reminders_muted"] for s in client.get("/api/projects").json()}
        assert listed == {"loud": True, "quiet": False}

        [(_to, _subject, body)] = send(mails, at("2027-01-29T09:00"))
        assert "testserver/p/quiet" in body and "testserver/p/loud" not in body
        assert [m["name"] for m in digest(at("2027-01-29T09:00")).muted] == ["loud"]

        assert client.put("/api/projects/loud/reminders", json={"muted": False}).json()["reminders_muted"] is False
        assert "testserver/p/loud" in send(mails, at("2027-01-30T09:00"))[0][2]
        assert client.put("/api/projects/loud/reminders", json={}).status_code == 422
        assert client.put("/api/projects/nope/reminders", json={"muted": True}).status_code == 404


def test_only_owners_and_editors_are_reminded(mails):
    with running_app() as client:
        sign_in(client)
        p = paper(client, "shared", [SUBMISSION])
        for email, role in (("e@example.com", "editor"), ("r@example.com", "reviewer"), ("v@example.com", "viewer")):
            r = client.post(f"/api/projects/{p['id']}/members", json={"email": email, "role": role})
            assert r.status_code == 201, r.text
        assert sorted(to for to, _s, _b in send(mails, at("2027-01-29T09:00"))) == ["didi@example.com", "e@example.com"]
        assert items(at("2027-01-29T09:00"), "v@example.com") == []
        # A viewer may still mute it for themselves, it changes nothing.
        with second_client("v@example.com") as viewer:
            assert viewer.put(f"/api/projects/{p['id']}/reminders", json={"muted": True}).status_code == 200


def test_closed_outcomes_and_the_dates_of_one_branch(mails):
    with running_app() as client:
        sign_in(client)
        p = paper(client, "p", [
            SUBMISSION,
            {"kind": "revision", "due_at": "2027-02-01T13:00", "tz": "UTC"},
            {"kind": "camera_ready", "due_at": "2027-02-01T14:00", "tz": "UTC"},
        ])
        now = at("2027-01-31T09:00")
        assert [k for _p, t, k in items(now) if t == "date"] == ["submission"]

        url = f"/api/projects/{p['id']}"
        client.patch(url, json={"outcome": "accepted"})
        assert [k for _p, t, k in items(now) if t == "date"] == ["submission", "camera_ready"]
        client.patch(url, json={"outcome": "revision"})
        assert [k for _p, t, k in items(now) if t == "date"] == ["submission", "revision"]
        for closed in ("rejected", "withdrawn", "not_submitted"):
            client.patch(url, json={"outcome": closed})
            assert items(now) == []
        assert send(mails, now) == []

        client.patch(url, json={"outcome": None, "archived": True})
        assert items(now) == []


def test_plans_are_reminded_and_marked(mails):
    with running_app() as client:
        sign_in(client)
        r = client.post("/api/projects", json={
            "planned": True, "conference": "ICT4S", "year": 2027,
            "dates": [{"kind": "submission", "date": "2027-02-10", "tz": "AoE"},
                      {"kind": "abstract", "date": "2027-01-10", "tz": "AoE"}],
        })
        assert r.status_code == 201, r.text
        # 09:00 UTC on 28 Jan is 21:00 on 27 Jan AoE, 14 days before 10 Feb.
        [(_to, subject, body)] = send(mails, at("2027-01-28T09:00"))
        assert subject == "Paper submission in 14 days for 2027-ict4s"
        assert "ICT4S 2027, still a plan\n- Paper submission in 14 days, 10 Feb 2027 (AoE).\n" in body
        # The abstract date passed, but a plan asks nothing.
        assert "Did you" not in body


def test_writing_start(mails):
    with running_app() as client:
        sign_in(client)
        paper(client, "w", [{"kind": "submission", "due_at": "2027-03-01T12:00", "tz": "UTC"}])
        # 30 days of writing start at 12:00 UTC on 30 Jan.
        assert send(mails, at("2027-01-29T09:00")) == []
        [(_to, subject, body)] = send(mails, at("2027-01-30T09:00"))
        assert subject == "Start writing w"
        assert ("- Start writing today. Paper submission is in 30 days, 1 Mar 2027 at 12:00 UTC, "
                "and writing takes 30 days.") in body
        assert send(mails, at("2027-01-31T09:00")) == []

        # Writing that starts at 06:00 on the next day of the user's own.
        paper(client, "w2", [{"kind": "submission", "due_at": "2027-03-03T06:00", "tz": "UTC"}])
        [item] = [i for p in digest(at("2027-01-31T09:00"), fresh=True).papers for i in p["items"]]
        assert item["text"].startswith("Start writing tomorrow.")
        assert (item["start"], item["writing_days"]) == ("2027-02-01T06:00:00Z", 30)


def test_new_questions_come_once(mails):
    with running_app() as client:
        sign_in(client)
        paper(client, "q", [{"kind": "abstract", "date": "2027-01-10", "tz": "AoE"},
                            {"kind": "supplementary", "date": "2027-01-02", "tz": "AoE"}])
        # On the first look the abstract is still ahead (11:59:59 UTC on 11 Jan)
        # and the supplementary deadline passed long before.
        [(_to, _subject, body)] = send(mails, at("2027-01-11T09:00"))
        assert "Abstract registration today" in body and "supplementary" not in body
        # The abstract passed since that mail.
        [(_to, subject, body)] = send(mails, at("2027-01-12T09:00"))
        assert subject == "A question about q"
        assert "- The abstract registration deadline passed on 10 Jan 2027. Did you register the abstract?" in body
        assert "http://testserver/p/q" in body
        assert send(mails, at("2027-01-13T09:00")) == []
        # The preview still lists every waiting question.
        assert [k for _p, t, k in items(at("2027-01-13T09:00")) if t == "question"] == ["supplementary", "abstract"]


def test_nothing_to_say_means_no_mail(mails):
    from app import reminders

    with running_app() as client:
        sign_in(client)
        assert send(mails, at("2027-01-29T09:00")) == []
        paper(client, "far", [{"kind": "submission", "due_at": "2028-02-01T12:00", "tz": "UTC"}])
        assert send(mails, at("2027-01-30T09:00")) == []
        # The day counts as looked at.
        assert _user().reminded_at == at("2027-01-30T09:00")
        assert reminders.render(_user(), []) is None


def test_one_failing_mail_does_not_stop_the_others(monkeypatch):
    from app import emailer, reminders

    with running_app() as client:
        sign_in(client)
        p = paper(client, "p", [SUBMISSION])
        client.post(f"/api/projects/{p['id']}/members", json={"email": "e@example.com", "role": "editor"})
        got: list[str] = []

        def flaky(to, subject, body):
            if to == EMAIL:
                raise RuntimeError("mail server on fire")
            got.append(to)

        monkeypatch.setattr(emailer, "send_email", flaky)
        assert reminders.send_due(at("2027-01-29T09:00")) == 1
        assert got == ["e@example.com"]
        # Nothing was recorded for the failed one, so the next round tries again.
        assert _user().reminded_at is None
        monkeypatch.setattr(emailer, "send_email", lambda to, subject, body: got.append(to))
        assert reminders.send_due(at("2027-01-29T09:05")) == 1
        assert got == ["e@example.com", EMAIL]


# --- Settings and the preview -------------------------------------------------------


def test_the_settings(mails):
    with running_app() as client:
        me = sign_in(client)
        assert (me["reminders"], me["reminder_days"], me["reminder_hour"]) == (True, [14, 3, 1, 0], 8)
        r = client.patch("/api/auth/me", json={"reminder_days": [7, 30, 7, 0], "reminder_hour": 18})
        assert r.status_code == 200, r.text
        assert (r.json()["reminder_days"], r.json()["reminder_hour"]) == ([30, 7, 0], 18)
        assert client.get("/api/auth/me").json()["reminder_days"] == [30, 7, 0]
        for bad in ({"reminder_hour": 24}, {"reminder_hour": -1}, {"reminder_days": [-1]},
                    {"reminder_days": [366]}, {"reminder_days": list(range(11))}):
            assert client.patch("/api/auth/me", json=bad).status_code == 422, bad
        assert client.patch("/api/auth/me", json={"reminder_days": []}).json()["reminder_days"] == []

        client.patch("/api/auth/me", json={"reminder_days": [3], "reminder_hour": 8, "reminders": False})
        paper(client, "p", [SUBMISSION])
        assert send(mails, at("2027-01-29T09:00")) == []
        assert client.patch("/api/auth/me", json={"reminders": True}).json()["reminders"] is True
        assert len(send(mails, at("2027-01-29T09:00"))) == 1


def test_the_preview_ignores_the_hour_and_what_was_sent(mails):
    from app import reminders

    with running_app() as client:
        sign_in(client)
        soon = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d")
        paper(client, "soon", [{"kind": "submission", "date": soon, "tz": "UTC"}])
        paper(client, "far", [{"kind": "submission", "date": "2099-01-01", "tz": "UTC"}])
        client.patch("/api/auth/me", json={"reminder_hour": 23})

        r = client.get("/api/reminders/preview")
        assert r.status_code == 200, r.text
        data = r.json()
        assert (data["reminders"], data["reminder_days"], data["reminder_hour"]) == (True, [14, 3, 1, 0], 23)
        assert data["sending"] is False  # REMINDERS_ENABLED is off in the tests
        [project] = data["projects"]
        assert project["name"] == "soon" and project["url"] == "http://testserver/p/soon"
        [item] = project["items"]
        assert (item["type"], item["kind"], item["threshold"]) == ("date", "submission", 3)
        assert item["days_left"] in (2, 3)  # 3 days in UTC, whatever the hour now
        assert data["subject"].startswith("Paper submission in ")
        assert "http://testserver/p/soon" in data["body"]
        assert data["next_at"] is not None and data["muted"] == []

        # After the mail went out, the preview still says the same.
        reminders.send_due(datetime.now(UTC).replace(tzinfo=None).replace(hour=23, minute=30))
        assert [p["name"] for p in client.get("/api/reminders/preview").json()["projects"]] == ["soon"]

        client.patch("/api/auth/me", json={"reminders": False})
        off = client.get("/api/reminders/preview").json()
        assert off["reminders"] is False and off["next_at"] is None


def test_the_loop_runs_only_when_enabled(monkeypatch):
    import anyio

    from app import reminders
    from app.config import get_settings

    calls: list[int] = []
    monkeypatch.setattr(reminders, "send_due", lambda now=None: calls.append(1) or 0)

    async def main():
        async with reminders.running():
            await anyio.sleep(0.5)

    anyio.run(main)
    assert calls == []
    monkeypatch.setattr(get_settings(), "reminders_enabled", True)
    anyio.run(main)
    assert calls == [1]


def test_start_writing_and_the_same_deadline_come_as_one_item(mails):
    with running_app() as client:
        sign_in(client)
        # 30 days of writing and a 30 day reminder meet on the same morning.
        assert client.patch("/api/auth/me", json={"reminder_days": [30, 14, 3, 1, 0]}).status_code == 200
        paper(client, "one", [{"kind": "submission", "due_at": "2027-03-01T12:00", "tz": "UTC"}])
        [(_to, subject, body)] = send(mails, at("2027-01-30T09:00"))
        assert subject == "Start writing one"
        assert body.count("Paper submission") == 1
        # The 30 day reminder of that date was recorded with it.
        assert send(mails, at("2027-01-31T09:00")) == []
