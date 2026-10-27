"""The review commands (DESIGN §13, CLI) against a fake HTTP API: the requests
they send and what they print for a person or for Claude."""

import io
import json

import httpx
import pytest
from meertex import cli, cmd_reviews
from test_cli import SERVER, FakeApi, run

P = "/api/projects/paper"
TID = "a1b2c3d4-0000-4000-8000-000000000001"
RID = "9f8e7d6c-0000-4000-8000-000000000002"
RITA = {"id": "u1", "email": "rita@example.com", "name": "Rita"}
DIDI = {"id": "u2", "email": "didi@example.com", "name": None}


def comment(cid: str, author: dict, body: str, suggestion: str | None = None, kind: str = "comment") -> dict:
    return {"id": cid, "author": author, "kind": kind, "body": body, "suggestion": suggestion,
            "created_at": "2026-10-27T10:00:00Z", "edited_at": None}


THREAD = {
    "id": TID, "review_id": RID, "state": "open", "pending": False, "created_by": RITA,
    "created_at": "2026-10-27T10:00:00Z", "updated_at": "2026-10-27T11:00:00Z", "file_id": "f1",
    "path": "sections/intro.tex", "start": 120, "end": 180, "start16": 121, "end16": 181,
    "line_start": 12, "line_end": 13, "quote": "We measure the energy\nof LLM inference.",
    "text": "We carefully measure the energy\nof LLM inference.", "changed": True, "outdated": False,
    "base": {"commit": "c0ffee00aa", "path": "intro.tex", "start": 100, "end": 160, "line_start": 10, "line_end": 11},
    "resolution": None, "accepted": None,
    "comments": [
        comment("c1aaaaaa-1", RITA, "Which energy? GPU only?"),
        comment("c2bbbbbb-2", DIDI, "GPU and CPU.", suggestion="We measure GPU and CPU energy\nof LLM inference."),
        comment("c3cccccc-3", DIDI, "Said so.", kind="resolved"),
        comment("c4dddddd-4", RITA, "", kind="reopened"),
    ],
}
REVIEW = {"id": RID, "reviewer": RITA, "state": "pending", "verdict": None, "summary": "", "commit": "c0ffee00aa",
          "version": {"id": "v1", "tag": "submission", "label": "Submission"}, "created_at": "2026-10-27T10:00:00Z",
          "submitted_at": None, "counts": {"open": 2, "resolved": 1, "accepted": 0}}


@pytest.fixture
def fake(monkeypatch):
    f = FakeApi()
    monkeypatch.setenv("MEERTEX_SERVER", SERVER)
    monkeypatch.setenv("MEERTEX_TOKEN", "tok")
    monkeypatch.setattr(cli, "api", lambda: f.api())
    return f


def test_parse_lines():
    assert cmd_reviews.parse_lines("main.tex:12") == ("main.tex", 12, 12)
    assert cmd_reviews.parse_lines("sections/a:b.tex:3-5") == ("sections/a:b.tex", 3, 5)
    for bad in ("main.tex", "main.tex:0", "main.tex:5-3", "main.tex:x", ":4"):
        with pytest.raises(cli.UsageError):
            cmd_reviews.parse_lines(bad)
    assert cmd_reviews.line_count("a\nb\n") == 2
    assert cmd_reviews.line_count("") == 1


def test_threads_print_what_an_agent_needs(fake, capsys):
    fake.routes[("GET", f"{P}/threads")] = [THREAD]
    code, out, _ = run(capsys, "threads", "paper")
    assert code == 0
    assert fake.calls[0][2] == {"state": "open"}
    lines = out.splitlines()
    assert lines[0] == "a1b2c3d4  open  sections/intro.tex:12-13  rita@example.com  review 9f8e7d6c"
    assert "    > We measure the energy" in lines and "    > of LLM inference." in lines
    now = lines.index("  now")
    assert lines[now + 1] == "    > We carefully measure the energy"
    assert "    Which energy? GPU only?" in lines
    suggestion = lines.index("    suggestion c2bbbbbb")
    assert lines[suggestion + 1: suggestion + 5] == [
        "      - We carefully measure the energy",
        "      - of LLM inference.",
        "      + We measure GPU and CPU energy",
        "      + of LLM inference.",
    ]
    assert any(line.startswith("  didi@example.com resolved it, ") for line in lines)
    assert "    Said so." in lines
    assert any(line.startswith("  rita@example.com reopened it, ") for line in lines)

    code, out, _ = run(capsys, "threads", "paper", "--state", "all", "--review", RID[:8], "--json")
    assert json.loads(out) == [THREAD]
    assert fake.calls[-1][2] == {"state": "all", "review": RID[:8]}


def test_outdated_and_resolved_threads(fake, capsys):
    gone = {**THREAD, "outdated": True, "changed": True, "text": "", "path": "intro.tex", "line_start": 10,
            "line_end": 11, "state": "resolved",
            "resolution": {"by": DIDI, "at": "2026-10-27T12:00:00Z", "commit": "beef0000aa", "note": "",
                           "diff": "@@ -10,2 +10 @@\n-We measure the energy\n~\n-of LLM inference.\n~\n"},
            "comments": [comment("c1aaaaaa-1", RITA, "Drop it?", suggestion="")]}
    fake.routes[("GET", f"{P}/threads/{TID[:8]}")] = gone
    code, out, _ = run(capsys, "thread", "paper", TID[:8])
    assert code == 0
    lines = out.splitlines()
    assert lines[0].endswith("outdated")
    assert "The text is gone. It was intro.tex:10-11 at commit c0ffee00." in out
    assert "  now" not in lines
    # The suggestion is against the quote when the text is gone, and it deletes.
    assert "      - We measure the energy" in lines and "      (deletes the text)" in lines
    assert "  change at commit beef0000" in lines
    assert "    [-We measure the energy-]" in lines


def test_threads_across_projects(fake, capsys):
    other = {**THREAD, "id": "b" * 8 + TID[8:], "project": {"id": "p2", "name": "beta", "title": "Beta paper"}}
    first = {**THREAD, "project": {"id": "p1", "name": "alpha", "title": "Alpha paper"}}
    fake.routes[("GET", "/api/threads")] = [first, other]
    code, out, _ = run(capsys, "threads")
    assert code == 0
    assert out.index("alpha  Alpha paper") < out.index("a1b2c3d4") < out.index("beta  Beta paper") < out.index("bbbbbbbb")
    code, _, err = run(capsys, "threads", "--review", RID)
    assert code == 2 and "need a project" in err
    fake.routes[("GET", "/api/threads")] = []
    assert run(capsys, "threads", "--state", "resolved")[1] == "No resolved threads.\n"


def test_reply_resolve_accept_reopen_apply(fake, capsys, monkeypatch):
    url = f"{P}/threads/{TID[:8]}"
    for action in ("comments", "resolve", "accept", "reopen", "apply"):
        fake.routes[("POST", f"{url}/{action}")] = (201 if action == "comments" else 200, THREAD)

    monkeypatch.setattr("sys.stdin", io.StringIO("A long\nreply."))
    code, out, _ = run(capsys, "reply", "paper", TID[:8], "-", "--suggestion", "Better text.")
    assert code == 0 and out == "Replied to a1b2c3d4 (sections/intro.tex:12-13), now open.\n"
    assert fake.calls[-1][3] == {"body": "A long\nreply.", "suggestion": "Better text."}
    code, _, err = run(capsys, "reply", "paper", TID[:8], "-", "--suggestion", "-")
    assert code == 2 and "Only one argument" in err

    assert run(capsys, "resolve", "paper", TID[:8], "--note", "Fixed in the checkout.")[0] == 0
    assert fake.calls[-1][1:] == (f"{url}/resolve", {}, {"note": "Fixed in the checkout."})
    assert run(capsys, "accept", "paper", TID[:8])[0] == 0
    assert fake.calls[-1][3] == {}
    assert run(capsys, "reopen", "paper", TID[:8], "--note", "Not yet.", "--json")[0] == 0
    assert fake.calls[-1][3] == {"note": "Not yet."}
    code, out, _ = run(capsys, "apply", "paper", TID[:8], "--comment", "c2bbbbbb")
    assert code == 0 and "Applied the suggestion to sections/intro.tex:12-13" in out
    assert fake.calls[-1][3] == {"comment_id": "c2bbbbbb"}

    fake.routes[("POST", f"{url}/accept")] = httpx.Response(403, json={"detail": "Only the person who started it"})
    code, _, err = run(capsys, "accept", "paper", TID[:8])
    assert code == 1 and "Only the person" in err


def test_comment_checks_the_live_lines(fake, capsys):
    live = "one\ntwo\nthree\n"
    fake.routes[("GET", f"{P}/files/sections/intro.tex")] = httpx.Response(
        200, text=live, headers={"content-type": "text/plain; charset=utf-8"})
    posted = {**THREAD, "quote": "two\nthree", "line_start": 2, "line_end": 3, "review_id": None}
    fake.routes[("POST", f"{P}/threads")] = (201, posted)

    code, out, _ = run(capsys, "comment", "paper", "sections/intro.tex:2-3", "Merge these?", "--suggestion", "two three")
    assert code == 0, out
    assert fake.calls[0][2] == {"ref": "live"}
    assert fake.calls[-1][3] == {"path": "sections/intro.tex", "ref": "live", "line_start": 2, "line_end": 3,
                                 "body": "Merge these?", "suggestion": "two three"}
    assert "Added thread a1b2c3d4 on sections/intro.tex:2-3." in out
    assert "  > two" in out.splitlines()

    fake.calls.clear()
    code, _, err = run(capsys, "comment", "paper", "sections/intro.tex:3-4", "x")
    assert code == 2 and "has 3 lines" in err
    assert [c[0] for c in fake.calls] == ["GET"]


def test_comment_into_a_review_starts_one_when_needed(fake, capsys):
    fake.routes[("GET", f"{P}/files/main.tex")] = httpx.Response(200, text="a\nb\n")
    fake.routes[("GET", f"{P}/reviews")] = []
    fake.routes[("POST", f"{P}/reviews")] = (201, REVIEW)
    fake.routes[("POST", f"{P}/threads")] = (201, {**THREAD, "pending": True})
    code, out, _ = run(capsys, "comment", "paper", "main.tex:1", "Hm.", "--review")
    assert code == 0
    assert [c[:2] for c in fake.calls] == [("GET", f"{P}/files/main.tex"), ("GET", f"{P}/reviews"),
                                          ("POST", f"{P}/reviews"), ("POST", f"{P}/threads")]
    assert fake.calls[2][3] == {"ref": "HEAD"}
    assert fake.calls[3][3]["review_id"] == RID
    assert "Started a review on HEAD" in out and "to your pending review" in out

    fake.calls.clear()
    fake.routes[("GET", f"{P}/reviews")] = [{**REVIEW, "state": "submitted"}, REVIEW]
    assert run(capsys, "comment", "paper", "main.tex:2", "Hm.", "--review")[0] == 0
    assert [c[:2] for c in fake.calls][-1] == ("POST", f"{P}/threads")
    assert ("POST", f"{P}/reviews") not in [c[:2] for c in fake.calls]


def test_review_start_submit_and_discard(fake, capsys):
    fake.routes[("POST", f"{P}/reviews")] = (201, REVIEW)
    code, out, _ = run(capsys, "review", "paper", "start", "--ref", "submission")
    assert code == 0 and out.startswith("Started review 9f8e7d6c on submission.")
    assert fake.calls[-1][3] == {"ref": "submission"}
    fake.routes[("POST", f"{P}/reviews")] = (200, REVIEW)
    assert "You have a pending review already, 9f8e7d6c" in run(capsys, "review", "paper", "start")[1]

    fake.routes[("GET", f"{P}/reviews")] = [REVIEW]
    submitted = {**REVIEW, "state": "submitted", "verdict": "changes_requested"}
    fake.routes[("POST", f"{P}/reviews/{RID}/submit")] = submitted
    code, out, _ = run(capsys, "review", "paper", "submit", "--verdict", "changes-requested", "--summary", "Two things.")
    assert code == 0
    assert fake.calls[-1][3] == {"verdict": "changes_requested", "summary": "Two things."}
    assert "Submitted review 9f8e7d6c (changes requested) with 3 threads." in out
    code, _, err = run(capsys, "review", "paper", "submit", "--verdict", "great")
    assert code == 2 and "approve, comment, changes_requested" in err

    fake.routes[("DELETE", f"{P}/reviews/{RID}")] = {"ok": True}
    code, out, _ = run(capsys, "review", "paper", "discard", "--json")
    assert code == 0 and json.loads(out) == {"discarded": RID}

    fake.routes[("GET", f"{P}/reviews")] = [submitted]
    code, _, err = run(capsys, "review", "paper", "discard")
    assert code == 2 and "no pending review" in err


def test_reviews_table(fake, capsys):
    done = {**REVIEW, "id": "1" * 8 + RID[8:], "state": "submitted", "verdict": "approve",
            "summary": "Looks good.", "submitted_at": "2026-10-27T10:00:00Z", "version": None}
    fake.routes[("GET", f"{P}/reviews")] = [REVIEW, done]
    code, out, _ = run(capsys, "reviews", "paper")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "STATE", "VERDICT", "REVIEWER", "ON", "OPEN", "RESOLVED", "ACCEPTED", "SUBMITTED"]
    assert lines[1].split()[:5] == ["9f8e7d6c", "pending", "rita@example.com", "submission", "2"]
    assert lines[2].split()[:5] == ["11111111", "submitted", "approve", "rita@example.com", "c0ffee00"]
    assert "  Looks good." in lines
    fake.routes[("GET", f"{P}/reviews")] = []
    assert "No reviews of paper yet" in run(capsys, "reviews", "paper")[1]
