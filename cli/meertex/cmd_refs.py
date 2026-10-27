"""``meertex refs`` (docs/DESIGN.md §16).

Checks a project's ``.bib`` entries against the publishers' records and the
web, so wrong and made-up citations show up before a reviewer finds them.
``meertex refs <project>`` prints the report, ``check`` starts a run and
follows it, ``add`` adds an entry from a DOI, an arXiv id, an ISBN or a link,
``mark`` and ``unmark`` set the checked mark, and ``set`` sets one field.

The report lists every entry that needs a look (all of them with ``--all``)
with its findings, so an agent can fix the ``.bib`` from it without another
request. The command exits 1 while an entry is ``error`` or ``changed``, or
while one cannot be parsed.

The commands live here rather than in cli.py, which only registers them, and
reach the API through ``cli.api()`` at call time, so the tests' fake API
applies to them as well.
"""

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable
from urllib.parse import quote

import httpx

from . import cli, output
from .api import project_path
from .output import paint, print_json, table

# How often ``check`` asks for the run's progress, in seconds.
POLL_SECONDS = 1.5
# Adding an entry looks the source up at doi.org, Open Library or the page itself.
ADD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
TITLE_LIMIT = 70
SENTENCE_LIMIT = 100

# The order the header counts them in, worst first.
STATUSES = ("error", "changed", "warning", "pending", "unverified", "checked", "verified")
STATUS_WIDTH = max(len(s) for s in STATUSES)
STATUS_COLORS = {
    "error": output.RED,
    "changed": output.AMBER,
    "warning": output.AMBER,
    "checked": output.GREEN,
    "verified": output.GREEN,
}
LEVEL_COLORS = {"error": output.RED, "warning": output.AMBER}
# Entries the report leaves out without --all.
FINE = ("verified", "checked")
# Entries that make the command exit 1.
BAD = ("error", "changed")


def refs_path(ref: str, key: str | None = None) -> str:
    """The references route of a project, or of one entry. Keys may hold
    ``/``, ``:`` or ``+``, so they are encoded completely."""
    path = project_path(ref) + "/references"
    return path if key is None else f"{path}/{quote(key, safe='')}"


# --- Printing ----------------------------------------------------------------------------


def _entries(n: int) -> str:
    return f"{n} entry" if n == 1 else f"{n} entries"


def _field(entry: dict, name: str) -> str:
    """A field of the entry. Field names are compared without case (§16)."""
    for key, value in (entry.get("fields") or {}).items():
        if key.lower() == name:
            return str(value or "")
    return ""


def _where(item: dict) -> str:
    path, line = item.get("path") or "", item.get("line")
    return f"{path}:{line}" if path and line else path


def _indented(text: str, first: str, rest: str) -> list[str]:
    lines = str(text or "").split("\n")
    return [(first + lines[0]).rstrip()] + [(rest + line).rstrip() for line in lines[1:]]


def run_text(run: dict | None) -> str:
    """The last run in one sentence, with its status, when and done/total."""
    if not run:
        return "No check has run yet."
    what = "check with claims" if run.get("claims") else "check"
    progress = f"{run.get('done') or 0}/{run.get('total') or 0}"
    status = run.get("status") or ""
    if status == "running":
        return f"A {what} is running, {progress}, started {output.ago(run.get('started_at'))}."
    label = paint(status, output.RED if status == "failed" else None)
    return f"Last {what} {label} {output.ago(run.get('finished_at') or run.get('started_at'))}, {progress}."


def header_text(entries: list[dict], run: dict | None) -> str:
    counts = Counter(e.get("status") for e in entries)
    parts = [paint(f"{counts[s]} {s}", STATUS_COLORS.get(s)) for s in STATUSES if counts[s]]
    parts += [f"{n} {s}" for s, n in counts.items() if s and s not in STATUSES]
    head = f"{_entries(len(entries))}: {', '.join(parts)}." if parts else f"{_entries(len(entries))}."
    return f"{head} {run_text(run)}"


def finding_lines(f: dict) -> list[str]:
    level = f.get("level") or ""
    color = LEVEL_COLORS.get(level)
    prefix = f"  {paint(f'{level:<7}', color)} "
    if f.get("code") == "claim":
        # The message is Claude's reason, value the citing sentence and
        # record_value the passage, which the report leaves to --json.
        lines = _indented(f"claim {f.get('verdict')}: {f.get('message') or ''}", prefix, "    ")
        where, sentence = _where(f), output.truncate(str(f.get("value") or ""), SENTENCE_LIMIT)
        if where and sentence:
            lines.append(f"    at {where}: {sentence}")
        elif where or sentence:
            lines.append(f"    at {where}" if where else f"    {sentence}")
        return lines
    lines = _indented(f"{f.get('code')}: {f.get('message') or ''}", prefix, "    ")
    if f.get("record_value") not in (None, ""):
        lines += _indented(f["record_value"], "    record: ", "            ")
    return lines


def entry_lines(entry: dict) -> list[str]:
    status = entry.get("status") or ""
    title = output.truncate(_field(entry, "title"), TITLE_LIMIT)
    head = (f"{paint(f'{status:<{STATUS_WIDTH}}', STATUS_COLORS.get(status))} {entry.get('key') or '(no key)'}"
            f"  {_where(entry)}  {title}")
    lines = [head.rstrip()]
    mark = entry.get("mark")
    if mark:
        since = "" if mark.get("valid") else ", and the entry changed since"
        lines.append(paint(f"  marked as checked on {mark.get('date')} by {mark.get('by')}{since}", output.DIM))
    for f in entry.get("findings") or []:
        lines += finding_lines(f)
    return lines


def summary_text(entries: list[dict], missing: list[dict], broken: list[dict]) -> str:
    counts = Counter(e.get("status") for e in entries)
    parts = []
    if counts["error"]:
        parts.append(output.count(counts["error"], "error"))
    if counts["changed"]:
        parts.append(f"{counts['changed']} changed")
    if counts["warning"]:
        parts.append(output.count(counts["warning"], "warning"))
    sentences = []
    if parts:
        sentences.append(f"{', '.join(parts)} in {_entries(len(entries))}.")
    if broken:
        sentences.append(f"{_entries(len(broken))} cannot be parsed.")
    keys = len({m.get("key") for m in missing})
    if keys:
        sentences.append("1 cited key has no entry." if keys == 1 else f"{keys} cited keys have no entry.")
    if sentences:
        return " ".join(sentences)
    if not entries:
        return "The project has no .bib entries."
    open_ = counts["pending"] + counts["unverified"]
    if open_:
        verb = "is" if open_ == 1 else "are"
        return f"Nothing is wrong in {_entries(len(entries))}, but {open_} {verb} not verified yet."
    return f"Nothing is wrong in {_entries(len(entries))}."


def exit_code(report: dict) -> int:
    bad = any(e.get("status") in BAD for e in report.get("entries") or [])
    return 1 if bad or report.get("broken") else 0


def print_report(report: dict, show_all: bool) -> None:
    entries = report.get("entries") or []
    missing = report.get("missing") or []
    broken = report.get("broken") or []
    print(header_text(entries, report.get("run")))
    if report.get("network_enabled") is False:
        print(paint("This server does not look references up, so only the checks without network apply.",
                    output.DIM))
    shown = [e for e in entries if show_all or e.get("status") not in FINE]
    if shown:
        print()
        for e in shown:
            print("\n".join(entry_lines(e)))
    if broken:
        print("\nBroken entries")
        print(table([[b.get("key") or "(no key)", _where(b), b.get("message") or ""] for b in broken], indent="  "))
    if missing:
        print("\nMissing keys")
        print(table([[m.get("key") or "", _where(m)] for m in missing], indent="  "))
    print()
    print(summary_text(entries, missing, broken))


def mark_text(entry: dict) -> str:
    mark = entry.get("mark")
    if not mark:
        what = "not marked"
    else:
        what = f"marked on {mark.get('date')} by {mark.get('by')}"
        if not mark.get("valid"):
            what += ", and the entry changed since"
    status = entry.get("status") or ""
    return f"{entry.get('key')}  {paint(status, STATUS_COLORS.get(status))}  {what}"


class Progress:
    """One line on stderr that updates in place, and only on a terminal."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stderr
        self.enabled = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.shown = False

    def show(self, run: dict) -> None:
        if not self.enabled:
            return
        what = "references and claims" if run.get("claims") else "references"
        self.stream.write(f"\rChecking {what} {run.get('done') or 0}/{run.get('total') or 0}\x1b[K")
        self.stream.flush()
        self.shown = True

    def clear(self) -> None:
        if self.shown:
            self.stream.write("\r\x1b[K")
            self.stream.flush()
            self.shown = False


# --- Commands ----------------------------------------------------------------------------


def cmd_show(args) -> int:
    with cli.api() as a:
        report = a.get(refs_path(cli.project_ref(args.project)))
    if args.json:
        print_json(report)
    else:
        print_report(report, args.all)
    return exit_code(report)


def cmd_check(args) -> int:
    base = refs_path(cli.project_ref(args.project))
    with cli.api() as a:
        resp = a.request("POST", base + "/check", json={"claims": args.claims, "refresh": args.refresh}, raw=True)
        run = resp.json()
        if resp.status_code == 200 and (run or {}).get("status") == "running" and not args.json:
            cli.note("A check is running already. This follows it.")
        progress = Progress()
        try:
            while run and run.get("status") == "running":
                progress.show(run)
                time.sleep(POLL_SECONDS)
                run = a.get(f"{base}/check/{quote(str(run['id']), safe='')}")
        finally:
            progress.clear()
        report = a.get(base)
    failed = (run or {}).get("status") == "failed"
    if args.json:
        print_json(report)
    else:
        print_report(report, args.all)
        if failed:
            error = (run.get("error") or "No reason was given").rstrip(".")
            cli.err(f"The check failed. {error}.")
    return 1 if failed else exit_code(report)


def cmd_add(args) -> int:
    body: dict = {"source": args.source, "dry_run": args.dry_run}
    if args.key:
        body["key"] = args.key
    if args.path:
        body["path"] = args.path
    with cli.api() as a:
        added = a.post(refs_path(cli.project_ref(args.project)), body, timeout=ADD_TIMEOUT)
    if args.json:
        print_json(added)
        return 0
    verb = "Would add" if args.dry_run else "Added"
    print(f"{verb} {added.get('key')} to {_where(added)}")
    print((added.get("bibtex") or "").rstrip("\n"))
    return 0


def _mark(args, method: str) -> int:
    with cli.api() as a:
        entry = a.request(method, refs_path(cli.project_ref(args.project), args.key) + "/checked")
    if args.json:
        print_json(entry)
    else:
        print(mark_text(entry))
    return 0


def cmd_mark(args) -> int:
    return _mark(args, "POST")


def cmd_unmark(args) -> int:
    return _mark(args, "DELETE")


def cmd_set(args) -> int:
    value = args.value
    if value == "-":
        # echo and heredocs end with a newline that is no part of the value.
        value = sys.stdin.read().rstrip("\r\n")
    with cli.api() as a:
        entry = a.post(refs_path(cli.project_ref(args.project), args.key) + "/fields",
                       {"field": args.field, "value": value})
    if args.json:
        print_json(entry)
    else:
        print(f"Set {args.field} of {entry.get('key') or args.key}.")
        print("\n".join(entry_lines(entry)))
    return 0


ACTIONS = {None: cmd_show, "check": cmd_check, "add": cmd_add, "mark": cmd_mark, "unmark": cmd_unmark,
           "set": cmd_set}


def cmd_refs(args) -> int:
    return ACTIONS[args.action](args)


# --- Parser ------------------------------------------------------------------------------


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    """Register ``refs`` with cli.py's ``cmd`` helper. ``common`` carries
    ``--json`` for its subcommands."""
    p = cmd(
        "refs",
        cmd_refs,
        "Check a project's .bib entries against the publishers' records and the web",
        # argparse shows an optional subcommand as if it were required.
        usage="%(prog)s [-h] [--json] [--all] project [{check,add,mark,unmark,set} ...]",
        epilog="Without a subcommand it prints every entry that needs a look, the broken entries, the missing keys "
               "and the last run. It exits 1 when an entry is error or changed or cannot be parsed, else 0.",
    )
    p.add_argument("project", help="name, id, or a checkout folder such as '.'")
    p.add_argument("--all", action="store_true", help="list the verified and checked entries too")
    sub = p.add_subparsers(dest="action", metavar="check|add|mark|unmark|set")

    s = sub.add_parser("check", parents=[common], help="look every entry up, show the progress and print the report",
                       description="Look every entry up, show the progress and print the report.")
    s.add_argument("--claims", action="store_true",
                   help="ask Claude too whether the citing sentences hold. This costs money.")
    s.add_argument("--refresh", action="store_true", help="look up again the entries that have a recent result")
    # SUPPRESS keeps "refs P --all check" working, since a subcommand's defaults win.
    s.add_argument("--all", action="store_true", default=argparse.SUPPRESS,
                   help="list the verified and checked entries too")

    s = sub.add_parser("add", parents=[common], help="add an entry from a DOI, an arXiv id, an ISBN or a link",
                       description="Add an entry from a DOI, an arXiv id, an ISBN or a link and print it.")
    s.add_argument("source", help="a DOI (also as a doi.org link), an arXiv id or link, an ISBN, or any other link")
    s.add_argument("--key", metavar="K", help="the entry's key. By default like vaswani2017attention")
    s.add_argument("--path", metavar="P",
                   help="the .bib file. By default the one with the most entries, or a new refs.bib")
    s.add_argument("--dry-run", action="store_true", help="only print the entry and write nothing")

    s = sub.add_parser("mark", parents=[common], help="mark an entry as checked by you",
                       description="Mark an entry as checked by you. The mark goes into the entry itself.")
    s.add_argument("key")
    s = sub.add_parser("unmark", parents=[common], help="remove an entry's checked mark",
                       description="Remove an entry's checked mark.")
    s.add_argument("key")

    s = sub.add_parser("set", parents=[common], help="set one field of an entry",
                       description="Set one field of an entry, replacing its value or appending it.")
    s.add_argument("key")
    s.add_argument("field")
    s.add_argument("value", help="the new value, - to read it from stdin")
