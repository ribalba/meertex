"""``meertex refs`` (DESIGN §16, CLI) against a fake HTTP API: the requests it
sends, what it prints for a person or for Claude, and its exit codes."""

import io
import json
from collections import Counter

import pytest
from meertex import cli, cmd_refs
from test_cli import SERVER, FakeApi, run

P = "/api/projects/paper"
R = f"{P}/references"
RUN_ID = "5e5e5e5e-0000-4000-8000-000000000001"
DIDI = {"id": "u1", "email": "didi@example.com", "name": "Didi Hoffmann"}
BIBTEX = "@article{vaswani2017attention,\n  title = {Attention Is All You Need},\n  year = {2017},\n}\n"


def finding(code: str, level: str, message: str, record_value=None, source: str = "bibtex") -> dict:
    return {"code": code, "level": level, "field": None, "message": message, "value": None,
            "record_value": record_value, "source": source, "url": None}


def claim(verdict: str, level: str, reason: str, sentence: str, passage: str = "") -> dict:
    return {**finding("claim", level, reason, record_value=passage, source="claude"), "value": sentence,
            "path": "sections/intro.tex", "line": 7, "verdict": verdict}


def entry(key: str, status: str, findings=(), line: int = 1, title: str = "A title", mark=None) -> dict:
    return {"key": key, "type": "article", "path": "refs.bib", "line": line, "end_line": line + 4,
            "fingerprint": "3f2a9c1b" + "0" * 56, "fields": {"title": title, "author": "Ann Author"},
            "status": status, "mark": mark, "record": None, "checked_at": None, "findings": list(findings),
            "claims_at": None, "cites": [{"path": "main.tex", "line": 3}]}


def a_run(status: str = "done", done: int = 6, total: int = 6, claims: bool = False, error=None) -> dict:
    return {"id": RUN_ID, "status": status, "claims": claims, "refresh": False, "total": total, "done": done,
            "error": error, "started_at": "2026-10-27T10:00:00Z",
            "finished_at": None if status == "running" else "2026-10-27T10:01:00Z", "created_by": DIDI}


def report(entries: list[dict], missing=(), run_=None, network: bool = True, broken=()) -> dict:
    counts = Counter(e["status"] for e in entries)
    problems = counts["error"] + counts["changed"] + counts["warning"] + len(broken) + len(missing)
    return {"run": run_, "claims_enabled": True, "network_enabled": network, "files": ["refs.bib"],
            "counts": dict(counts), "problems": problems,
            "missing": list(missing), "broken": list(broken), "entries": entries}


SENTENCE = ("Lee et al. show that the energy of LLM inference falls by half on every GPU and CPU they "
            "measured, which we build on.")
LONG = "Attention is all we need, and a title so long that the report has to shorten it somewhere"
MARK = {"date": "2026-10-27", "by": "Didi Hoffmann", "hash": "3f2a9c1b", "valid": True}
ENTRIES = [
    entry("vaswani2017attention", "error", line=12, title=LONG, findings=[
        finding("doi_not_found", "error", "doi.org does not know 10.1234/nope.", source="doi.org"),
        finding("title", "warning", "The title differs from the record's.", record_value="Attention Is All You Need",
                source="crossref"),
    ]),
    entry("smith2020", "changed", line=20, mark={**MARK, "hash": "00000000", "valid": False}),
    entry("lee2021", "warning", line=30, findings=[
        claim("not_supported", "warning", "The paper measures GPUs only.", SENTENCE, "We measure the GPU."),
    ]),
    entry("kim2022", "pending", line=40),
    entry("good2019", "verified", line=50, findings=[
        finding("not_cited", "info", "No \\cite uses the key."),
        claim("no_source", "info", "There is no abstract to judge from.", "Short sentence."),
    ]),
    entry("done2018", "checked", line=60, mark=MARK),
]
MISSING = [{"key": "ghost2020", "path": "main.tex", "line": 12}]
BROKEN = [{"path": "refs.bib", "line": 70, "key": "bad2020", "message": "The entry has no closing brace."},
          {"path": "more.bib", "line": 3, "key": None, "message": "No key after @article{."}]


@pytest.fixture
def fake(monkeypatch):
    f = FakeApi()
    monkeypatch.setenv("MEERTEX_SERVER", SERVER)
    monkeypatch.setenv("MEERTEX_TOKEN", "tok")
    monkeypatch.setattr(cli, "api", lambda: f.api())
    return f


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(cmd_refs.time, "sleep", waited.append)
    return waited


# --- meertex refs <project> --------------------------------------------------------------


def test_show_lists_what_needs_a_look_and_exits_1(fake, capsys):
    fake.routes[("GET", R)] = report(ENTRIES, MISSING, a_run(), broken=BROKEN)
    code, out, err = run(capsys, "refs", "paper")
    assert code == 1 and err == ""
    assert fake.calls[0][:2] == ("GET", R)
    lines = out.splitlines()
    assert lines[0].startswith("6 entries: 1 error, 1 changed, 1 warning, 1 pending, 1 checked, 1 verified. "
                               "Last check done ")
    assert lines[0].endswith(", 6/6.")
    title = cmd_refs.output.truncate(LONG, 70)
    assert len(title) == 70 and title.endswith("…")
    assert f"error      vaswani2017attention  refs.bib:12  {title}" in lines
    assert "  error   doi_not_found: doi.org does not know 10.1234/nope." in lines
    assert "  warning title: The title differs from the record's." in lines
    assert "    record: Attention Is All You Need" in lines
    assert "changed    smith2020  refs.bib:20  A title" in lines
    assert "  marked as checked on 2026-10-27 by Didi Hoffmann, and the entry changed since" in lines
    # A claim shows its verdict, its reason and where the sentence is, without the passage.
    claim_at = lines.index("warning    lee2021  refs.bib:30  A title") + 1
    sentence = cmd_refs.output.truncate(SENTENCE, 100)
    assert len(sentence) == 100 and sentence.endswith("…")
    assert lines[claim_at: claim_at + 2] == ["  warning claim not_supported: The paper measures GPUs only.",
                                             f"    at sections/intro.tex:7: {sentence}"]
    assert lines[claim_at + 2] == "pending    kim2022  refs.bib:40  A title"
    assert "We measure the GPU." not in out
    assert "pending    kim2022  refs.bib:40  A title" in lines
    assert "good2019" not in out and "done2018" not in out
    broken = lines.index("Broken entries")
    assert lines[broken + 1: broken + 3] == ["  bad2020   refs.bib:70  The entry has no closing brace.",
                                             "  (no key)  more.bib:3   No key after @article{."]
    missing = lines.index("Missing keys")
    assert lines[missing + 1] == "  ghost2020  main.tex:12"
    assert lines[-1] == ("1 error, 1 changed, 1 warning in 6 entries. 2 entries cannot be parsed. "
                         "1 cited key has no entry.")


def test_show_all_json_and_exit_codes(fake, capsys):
    fake.routes[("GET", R)] = report(ENTRIES, MISSING, a_run())
    code, out, _ = run(capsys, "refs", "paper", "--all")
    assert code == 1
    lines = out.splitlines()
    assert "verified   good2019  refs.bib:50  A title" in lines
    assert "  info    not_cited: No \\cite uses the key." in lines
    assert "  info    claim no_source: There is no abstract to judge from." in lines
    assert "    at sections/intro.tex:7: Short sentence." in lines
    checked = lines.index("checked    done2018  refs.bib:60  A title")
    assert lines[checked + 1] == "  marked as checked on 2026-10-27 by Didi Hoffmann"

    code, out, _ = run(capsys, "refs", "paper", "--json")
    assert code == 1 and json.loads(out) == report(ENTRIES, MISSING, a_run())

    # Warnings and missing keys alone do not fail.
    fake.routes[("GET", R)] = report(ENTRIES[2:], MISSING, a_run())
    code, out, _ = run(capsys, "refs", "paper")
    assert code == 0
    assert out.splitlines()[-1] == "1 warning in 4 entries. 1 cited key has no entry."

    fake.routes[("GET", R)] = report(ENTRIES[4:], run_=a_run())
    code, out, _ = run(capsys, "refs", "paper")
    assert code == 0
    assert out.splitlines() == [out.splitlines()[0], "", "Nothing is wrong in 2 entries."]

    # A broken entry fails on its own.
    fake.routes[("GET", R)] = report(ENTRIES[4:], run_=a_run(), broken=BROKEN[:1])
    code, out, _ = run(capsys, "refs", "paper")
    assert code == 1
    assert out.splitlines()[-1] == "1 entry cannot be parsed."

    fake.routes[("GET", R)] = report([entry("kim2022", "pending")], network=False)
    code, out, _ = run(capsys, "refs", "paper")
    assert code == 0
    assert out.startswith("1 entry: 1 pending. No check has run yet.\n")
    assert "only the checks without network apply" in out
    assert out.splitlines()[-1] == "Nothing is wrong in 1 entry, but 1 is not verified yet."

    fake.routes[("GET", R)] = report([])
    code, out, _ = run(capsys, "refs", "paper")
    assert code == 0 and out.splitlines()[-1] == "The project has no .bib entries."


# --- meertex refs <project> check --------------------------------------------------------


def _progress(fake, states: list[dict]) -> None:
    left = list(states)
    fake.routes[("GET", f"{R}/check/{RUN_ID}")] = lambda request: left.pop(0) if len(left) > 1 else left[0]


def test_check_polls_until_the_run_is_done(fake, capsys, sleeps):
    fake.routes[("POST", f"{R}/check")] = (202, a_run("running", 0, 6, claims=True))
    _progress(fake, [a_run("running", 2, 6, claims=True), a_run("running", 5, 6, claims=True),
                     a_run("done", claims=True)])
    fake.routes[("GET", R)] = report(ENTRIES, MISSING, a_run(claims=True))

    code, out, err = run(capsys, "refs", "paper", "check", "--claims")
    assert code == 1
    assert err == ""  # no progress when stderr is not a terminal
    assert fake.calls[0][:2] == ("POST", f"{R}/check")
    assert fake.calls[0][3] == {"claims": True, "refresh": False}
    assert [c[:2] for c in fake.calls[1:]] == [("GET", f"{R}/check/{RUN_ID}")] * 3 + [("GET", R)]
    assert sleeps == [1.5, 1.5, 1.5]
    assert out.splitlines()[0].startswith("6 entries: 1 error, ")
    assert "Last check with claims done " in out.splitlines()[0]
    assert "good2019" not in out

    fake.calls.clear()
    sleeps.clear()
    fake.routes[("POST", f"{R}/check")] = (202, a_run("running", 0, 2))
    _progress(fake, [a_run("done", 2, 2)])
    fake.routes[("GET", R)] = report(ENTRIES[4:], run_=a_run("done", 2, 2))
    code, out, _ = run(capsys, "refs", "paper", "check", "--refresh", "--all", "--json")
    assert code == 0
    assert fake.calls[0][3] == {"claims": False, "refresh": True}
    assert json.loads(out) == report(ENTRIES[4:], run_=a_run("done", 2, 2))
    assert sleeps == [1.5]

    # --all before the subcommand works as well.
    code, out, _ = run(capsys, "refs", "paper", "--all", "check")
    assert code == 0 and "good2019" in out


def test_check_shows_one_progress_line_on_a_terminal(fake, capsys, sleeps, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    tty = Terminal()
    monkeypatch.setattr("sys.stderr", tty)
    fake.routes[("POST", f"{R}/check")] = (200, a_run("running", 1, 4))
    _progress(fake, [a_run("running", 3, 4), a_run("done", 4, 4)])
    fake.routes[("GET", R)] = report(ENTRIES[4:], run_=a_run("done", 4, 4))
    code, out, _ = run(capsys, "refs", "paper", "check")
    assert code == 0
    written = tty.getvalue()
    # The POST answered 200, so a run was going already.
    assert written.startswith("A check is running already. This follows it.\n")
    assert "\rChecking references 1/4\x1b[K\rChecking references 3/4\x1b[K\r\x1b[K" in written
    assert written.endswith("\r\x1b[K")
    assert "Checking" not in out


def test_a_failed_run_and_a_503(fake, capsys, sleeps):
    failed = a_run("failed", 1, 6, error="The server restarted during the check")
    fake.routes[("POST", f"{R}/check")] = (202, a_run("running", 0, 6))
    _progress(fake, [failed])
    fake.routes[("GET", R)] = report(ENTRIES[4:], run_=failed)
    code, out, err = run(capsys, "refs", "paper", "check")
    assert code == 1
    assert "Last check failed " in out.splitlines()[0]
    assert "The check failed. The server restarted during the check." in err

    code, out, _ = run(capsys, "refs", "paper", "check", "--json")
    assert code == 1 and json.loads(out)["run"]["status"] == "failed"

    fake.calls.clear()
    detail = "Reference lookups are off on this server. REFERENCES_NETWORK is not set."
    fake.routes[("POST", f"{R}/check")] = (503, {"detail": detail})
    code, out, err = run(capsys, "refs", "paper", "check", "--claims")
    assert code == 1 and out == ""
    assert detail in err
    assert [c[0] for c in fake.calls] == ["POST"]

    code, out, _ = run(capsys, "refs", "paper", "check", "--json")
    assert code == 1 and json.loads(out)["status"] == 503 and json.loads(out)["detail"] == detail


# --- add, mark, unmark, set --------------------------------------------------------------


def test_add_and_dry_run(fake, capsys):
    fake.routes[("POST", R)] = (201, {"key": "vaswani2017attention", "path": "refs.bib", "line": 42,
                                      "bibtex": BIBTEX})
    code, out, _ = run(capsys, "refs", "paper", "add", "10.48550/arXiv.1706.03762")
    assert code == 0
    assert fake.calls[-1][1:] == (R, {}, {"source": "10.48550/arXiv.1706.03762", "dry_run": False})
    assert out == "Added vaswani2017attention to refs.bib:42\n" + BIBTEX

    fake.routes[("POST", R)] = {"key": "attn", "path": "more.bib", "line": 7, "bibtex": BIBTEX}
    code, out, _ = run(capsys, "refs", "paper", "add", "https://arxiv.org/abs/1706.03762", "--key", "attn",
                       "--path", "more.bib", "--dry-run")
    assert code == 0
    assert fake.calls[-1][3] == {"source": "https://arxiv.org/abs/1706.03762", "dry_run": True, "key": "attn",
                                 "path": "more.bib"}
    assert out.startswith("Would add attn to more.bib:7\n@article{")

    code, out, _ = run(capsys, "refs", "paper", "add", "10.1/x", "--json")
    assert code == 0 and json.loads(out)["bibtex"] == BIBTEX

    fake.routes[("POST", R)] = (422, {"detail": "The key attn is taken."})
    code, _, err = run(capsys, "refs", "paper", "add", "10.1/x", "--key", "attn")
    assert code == 1 and "The key attn is taken." in err


def test_mark_and_unmark(fake, capsys):
    url = f"{R}/vaswani2017attention/checked"
    fake.routes[("POST", url)] = entry("vaswani2017attention", "checked", mark=MARK)
    fake.routes[("DELETE", url)] = entry("vaswani2017attention", "verified")

    code, out, _ = run(capsys, "refs", "paper", "mark", "vaswani2017attention")
    assert code == 0 and fake.calls[-1][:2] == ("POST", url)
    assert out == "vaswani2017attention  checked  marked on 2026-10-27 by Didi Hoffmann\n"

    code, out, _ = run(capsys, "refs", "paper", "unmark", "vaswani2017attention")
    assert code == 0 and fake.calls[-1][:2] == ("DELETE", url)
    assert out == "vaswani2017attention  verified  not marked\n"

    code, out, _ = run(capsys, "refs", "paper", "unmark", "vaswani2017attention", "--json")
    assert code == 0 and json.loads(out) == entry("vaswani2017attention", "verified")

    fake.routes[("POST", url)] = (409, {"detail": "Two entries have the key vaswani2017attention."})
    code, _, err = run(capsys, "refs", "paper", "mark", "vaswani2017attention")
    assert code == 1 and "Two entries have the key" in err


def test_set_a_field_also_from_stdin(fake, capsys, monkeypatch):
    url = f"{R}/lee2021/fields"
    fake.routes[("POST", url)] = entry("lee2021", "pending", line=30, title="New title")
    code, out, _ = run(capsys, "refs", "paper", "set", "lee2021", "title", "New title")
    assert code == 0
    assert fake.calls[-1][1:] == (url, {}, {"field": "title", "value": "New title"})
    assert out == "Set title of lee2021.\npending    lee2021  refs.bib:30  New title\n"

    monkeypatch.setattr("sys.stdin", io.StringIO("A long\nabstract.\n"))
    code, out, _ = run(capsys, "refs", "paper", "set", "lee2021", "abstract", "-", "--json")
    assert code == 0
    assert fake.calls[-1][3] == {"field": "abstract", "value": "A long\nabstract."}
    assert json.loads(out)["key"] == "lee2021"


def test_keys_are_url_encoded(capsys, monkeypatch):
    class RawFake(FakeApi):
        """Also records the path as it went over the wire."""

        def __init__(self):
            super().__init__()
            self.raw: list[tuple[str, str]] = []

        def handler(self, request):
            self.raw.append((request.method, request.url.raw_path.decode()))
            return super().handler(request)

    fake = RawFake()
    monkeypatch.setenv("MEERTEX_SERVER", SERVER)
    monkeypatch.setenv("MEERTEX_TOKEN", "tok")
    monkeypatch.setattr(cli, "api", lambda: fake.api())
    key = "smith:2020/a+b c#1?"
    encoded = "smith%3A2020%2Fa%2Bb%20c%231%3F"
    assert cmd_refs.refs_path("paper", key) == f"{R}/{encoded}"
    for method in ("POST", "DELETE"):
        fake.routes[(method, f"{R}/{key}/checked")] = entry(key, "checked", mark=MARK)
    fake.routes[("POST", f"{R}/{key}/fields")] = entry(key, "pending")

    assert run(capsys, "refs", "paper", "mark", key)[0] == 0
    assert run(capsys, "refs", "paper", "unmark", key)[0] == 0
    assert run(capsys, "refs", "paper", "set", key, "year", "2020")[0] == 0
    assert fake.raw == [("POST", f"{R}/{encoded}/checked"), ("DELETE", f"{R}/{encoded}/checked"),
                        ("POST", f"{R}/{encoded}/fields")]
