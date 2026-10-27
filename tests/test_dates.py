"""Dates, time zones, moving a date and the next date (DESIGN §5)."""

from helpers import needs_db, running_app, second_client, sign_in

pytestmark = needs_db

DATE_KEYS = {
    "id", "kind", "label", "due_at", "tz", "date_only", "local", "passed", "seconds_left", "answer", "answered_at",
    "changes",
}


def _project(client, **body) -> dict:
    r = client.post("/api/projects", json={"name": "paper", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_add_dates_in_every_kind_of_zone():
    with running_app() as client:
        sign_in(client)
        p = _project(client)
        url = f"/api/projects/{p['id']}/dates"

        r = client.post(url, json={"kind": "submission", "date": "2099-01-15"})
        assert r.status_code == 201, r.text
        d = r.json()
        assert set(d) == DATE_KEYS
        assert d["label"] == "Paper submission"
        assert d["tz"] == "AoE"
        assert d["date_only"] is True
        assert d["due_at"] == "2099-01-16T11:59:59Z"
        assert d["local"] == "2099-01-15T23:59:59-12:00"
        assert d["passed"] is False
        assert d["seconds_left"] > 0
        assert d["answer"] is None and d["answered_at"] is None and d["changes"] == []

        d = client.post(url, json={"kind": "abstract", "due_at": "2099-01-08T14:00", "tz": "utc+2"}).json()
        assert d["tz"] == "UTC+2"
        assert d["date_only"] is False
        assert d["due_at"] == "2099-01-08T12:00:00Z"
        assert d["local"] == "2099-01-08T14:00:00+02:00"

        d = client.post(url, json={"kind": "notification", "due_at": "2099-03-01T09:00:00-05:00", "tz": "Europe/Berlin"}).json()
        assert d["due_at"] == "2099-03-01T14:00:00Z"
        assert d["local"] == "2099-03-01T15:00:00+01:00"

        d = client.post(url, json={"kind": "other", "label": "Artifact evaluation", "date": "2020-05-01", "tz": "UTC"}).json()
        assert d["passed"] is True
        assert d["seconds_left"] < 0

        assert client.post(url, json={"kind": "other", "date": "2099-01-01"}).status_code == 422
        assert client.post(url, json={"kind": "submission", "date": "2099-01-01", "tz": "Mars/Olympus"}).status_code == 422
        assert client.post(url, json={"kind": "submission"}).status_code == 422
        assert client.post(url, json={"kind": "deadline", "date": "2099-01-01"}).status_code == 422
        assert client.post(url, json={"kind": "submission", "date": "15.01.2099"}).status_code == 422

        dates = client.get(f"/api/projects/{p['id']}").json()["dates"]
        assert [x["kind"] for x in dates] == ["other", "abstract", "submission", "notification"]


def test_moving_records_and_clears_the_answer():
    with running_app() as client:
        sign_in(client)
        p = _project(client, dates=[{"kind": "abstract", "date": "2026-01-10"}])
        d = p["dates"][0]
        client.post(f"/api/projects/{p['id']}/dates/{d['id']}/answer", json={"answer": "yes"})
        url = f"/api/projects/{p['id']}/dates/{d['id']}"

        # A label change is not a move.
        r = client.patch(url, json={"label": "Abstract (mandatory)"})
        assert r.json()["changes"] == []
        assert r.json()["answer"] == "yes"

        r = client.patch(url, json={"date": "2026-01-17"})
        moved = r.json()
        assert moved["answer"] is None
        assert moved["answered_at"] is None
        [change] = moved["changes"]
        assert change["by"] == "didi@example.com"
        assert change["from"] == "2026-01-11T11:59:59Z"
        assert change["to"] == "2026-01-18T11:59:59Z"
        assert change["at"].endswith("Z")

        # Only the zone changes. The same day is now read in UTC.
        moved = client.patch(url, json={"tz": "UTC"}).json()
        assert moved["due_at"] == "2026-01-17T23:59:59Z"
        assert moved["date_only"] is True
        assert len(moved["changes"]) == 2

        # "moved" as an answer does the same.
        p = client.post(f"{url}/answer", json={"answer": "moved", "due_at": "2099-01-20T12:00:00Z"}).json()
        [d] = p["dates"]
        assert d["due_at"] == "2099-01-20T12:00:00Z"
        assert d["answer"] is None
        assert len(d["changes"]) == 3
        assert client.post(f"{url}/answer", json={"answer": "moved"}).status_code == 422

        # Changing the kind clears the old question's answer and the default label goes with it.
        client.patch(url, json={"label": None})
        d = client.patch(url, json={"kind": "submission"}).json()
        assert d["kind"] == "submission"
        assert d["label"] == "Paper submission"

        assert client.delete(url).status_code == 200
        assert client.patch(url, json={"label": "x"}).status_code == 404
        assert client.get(f"/api/projects/{p['id']}").json()["dates"] == []


def test_next_date_skips_what_cannot_matter():
    with running_app() as client:
        sign_in(client)
        p = _project(
            client,
            dates=[
                {"kind": "camera_ready", "date": "2099-01-01"},
                {"kind": "revision", "date": "2099-01-02"},
                {"kind": "notification", "date": "2099-01-03"},
                {"kind": "conference_start", "date": "2099-06-01"},
            ],
        )
        url = f"/api/projects/{p['id']}"
        assert p["next_date"]["kind"] == "notification"
        assert set(p["next_date"]) == {"id", "kind", "label", "due_at", "tz", "date_only", "local", "seconds_left"}
        assert client.patch(url, json={"outcome": "revision"}).json()["next_date"]["kind"] == "revision"
        assert client.patch(url, json={"outcome": "accepted"}).json()["next_date"]["kind"] == "camera_ready"
        for closed in ("rejected", "withdrawn", "not_submitted"):
            assert client.patch(url, json={"outcome": closed}).json()["next_date"] is None
        client.patch(url, json={"outcome": None})
        notification = next(d for d in p["dates"] if d["kind"] == "notification")
        client.post(f"{url}/dates/{notification['id']}/answer", json={"answer": "accepted"})
        # Answered dates are not next either.
        assert client.get(url).json()["next_date"]["kind"] == "camera_ready"


def test_dates_need_an_editor():
    with running_app() as client:
        sign_in(client)
        p = _project(client, dates=[{"kind": "submission", "date": "2099-01-01"}])
        d = p["dates"][0]
        client.post(f"/api/projects/{p['id']}/members", json={"email": "vic@example.com", "role": "reviewer"})
        with second_client("vic@example.com") as vic:
            base = f"/api/projects/{p['id']}/dates"
            assert vic.post(base, json={"kind": "abstract", "date": "2099-01-01"}).status_code == 403
            assert vic.patch(f"{base}/{d['id']}", json={"date": "2099-01-02"}).status_code == 403
            assert vic.delete(f"{base}/{d['id']}").status_code == 403
            assert vic.post(f"{base}/{d['id']}/answer", json={"answer": "yes"}).status_code == 403
        with second_client("eve@example.com") as eve:
            assert eve.post(f"/api/projects/{p['id']}/dates", json={"kind": "abstract", "date": "2099-01-01"}).status_code == 404


def test_next_date_carries_the_venues_own_day():
    # 23:59:59 AoE on 15 Nov is 11:59:59 UTC on 16 Nov. The list must say 15 Nov.
    with running_app() as client:
        sign_in(client)
        p = client.post(
            "/api/projects",
            json={"name": "aoe", "dates": [{"kind": "submission", "date": "2099-11-15", "tz": "AoE"}]},
        ).json()
        nd = p["next_date"]
        assert nd["due_at"] == "2099-11-16T11:59:59Z"
        assert nd["local"] == "2099-11-15T23:59:59-12:00"
