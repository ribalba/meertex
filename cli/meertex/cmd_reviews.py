"""The review commands of ``meertex`` (docs/DESIGN.md §13, CLI).

Reviews work like GitHub's pull request reviews. A reviewer comments on lines
of the paper, the authors answer, change the text and resolve each thread, and
the reviewer accepts or reopens. Here they are commands for people and for
Claude alike.

Without ``--json`` a thread is printed so that an agent can work from it
without another request: its short id, state, who started it, where its text
is now (``path:line_start-line_end``, the same form ``meertex comment`` takes),
the quoted text, the current text when it changed, the whole conversation, and
every suggestion as a small diff against the current text.

The commands live here rather than in cli.py, which only registers them. They
reach the API through ``cli.api()`` at call time, so the tests' fake API
applies to them as well.
"""

import argparse
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from . import cli, output
from .api import file_path, project_path
from .output import paint, print_json, table

VERDICTS = ("approve", "comment", "changes_requested")
STATES = ("open", "resolved", "accepted", "all")
STATE_COLORS = {"open": output.AMBER, "resolved": output.GREEN, "accepted": output.GREEN}
EVENTS = {
    "resolved": "resolved it",
    "accepted": "accepted it",
    "reopened": "reopened it",
    "applied": "applied a suggestion",
}


# --- Small helpers ---------------------------------------------------------------------


def short(value: str | None) -> str:
    return (value or "")[:8]


def who(p: dict | None) -> str:
    return (p or {}).get("email") or "somebody"


def lines_label(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


def where(t: dict) -> str:
    return f"{t.get('path')}:{lines_label(t.get('line_start', 0), t.get('line_end', 0))}"


def thread_url(ref: str, thread_id: str) -> str:
    return f"{project_path(ref)}/threads/{quote(thread_id, safe='')}"


class _Stdin:
    """Lets one text argument be ``-``, read from stdin, for long or multi-line
    comments and suggestions."""

    def __init__(self) -> None:
        self.used = False

    def __call__(self, value: str | None) -> str | None:
        if value != "-":
            return value
        if self.used:
            raise cli.UsageError("Only one argument can be - (read from stdin).")
        self.used = True
        return sys.stdin.read()


def parse_lines(spec: str) -> tuple[str, int, int]:
    """``path:line`` or ``path:first-last`` as (path, first, last)."""
    path, sep, lines = spec.rpartition(":")
    first, dash, last = lines.partition("-")
    try:
        start = int(first)
        end = int(last) if dash else start
    except ValueError:
        start = end = 0
    if not sep or not path or start < 1 or end < start:
        raise cli.UsageError(f"{spec} is not a place in a file. Use PATH:LINE or PATH:FIRST-LAST, for example "
                             "main.tex:12 or sections/intro.tex:12-14.")
    return path, start, end


def line_count(text: str) -> int:
    """Lines as an editor numbers them, as the server counts them."""
    count = text.count("\n") + 1
    return count - 1 if count > 1 and text.endswith("\n") else count


def pending_review(a, ref: str) -> dict | None:
    """Your pending review of the project. Only your own pending one is
    listed among the reviews."""
    reviews = a.get(project_path(ref) + "/reviews") or []
    return next((r for r in reviews if r.get("state") == "pending"), None)


# --- Printing ----------------------------------------------------------------------------


def _block(text: str, indent: str = "    ") -> list[str]:
    if text == "":
        return [paint(f"{indent}(empty)", output.DIM)]
    return [f"{indent}> {line}".rstrip() for line in text.split("\n")]


def suggestion_lines(current: str, suggestion: str, indent: str = "      ") -> list[str]:
    """The suggestion as a small diff, the current text and then what replaces it."""
    lines = [paint(f"{indent}- {line}".rstrip(), output.RED) for line in current.split("\n")] if current else []
    if suggestion:
        lines += [paint(f"{indent}+ {line}".rstrip(), output.GREEN) for line in suggestion.split("\n")]
    else:
        lines.append(paint(f"{indent}(deletes the text)", output.DIM))
    return lines


def thread_lines(t: dict) -> list[str]:
    head = [paint(short(t.get("id")), output.BOLD), paint(t.get("state") or "", STATE_COLORS.get(t.get("state"))),
            where(t), who(t.get("created_by"))]
    if t.get("review_id"):
        head.append(f"review {short(t['review_id'])}" + (" (pending)" if t.get("pending") else ""))
    if t.get("outdated"):
        head.append(paint("outdated", output.RED))
    lines = ["  ".join(head)]
    if t.get("outdated"):
        base = t.get("base") or {}
        lines.append(paint(f"  The text is gone. It was {base.get('path')}:"
                           f"{lines_label(base.get('line_start', 0), base.get('line_end', 0))} "
                           f"at commit {short(base.get('commit'))}.", output.DIM))
    lines.append("  quote")
    lines += _block(t.get("quote") or "")
    # A suggestion replaces the text now, or the quote when the text is gone.
    current = (t.get("quote") if t.get("outdated") else t.get("text")) or ""
    if t.get("changed") and not t.get("outdated"):
        lines.append("  now")
        lines += _block(current)
    for c in t.get("comments") or []:
        when = output.ago(c.get("created_at"))
        if c.get("kind") == "comment":
            edited = ", edited" if c.get("edited_at") else ""
            lines.append(paint(f"  {who(c.get('author'))}, {when}{edited}", output.DIM))
            lines += [f"    {line}".rstrip() for line in (c.get("body") or "").split("\n") if c.get("body")]
            if c.get("suggestion") is not None:
                lines.append(f"    suggestion {short(c.get('id'))}")
                lines += suggestion_lines(current, c["suggestion"])
        else:
            verb = EVENTS.get(c.get("kind"), c.get("kind"))
            lines.append(paint(f"  {who(c.get('author'))} {verb}, {when}", output.DIM))
            if c.get("body"):
                lines += [f"    {line}".rstrip() for line in c["body"].split("\n")]
    resolution = t.get("resolution") or {}
    if resolution.get("diff"):
        lines.append(f"  change at commit {short(resolution.get('commit'))}")
        diff = cli._word_lines(resolution["diff"])
        lines += ["    " + line for line in diff if not output.ANSI.sub("", line).startswith("@@")]
    return lines


def print_threads(threads: list[dict], state: str) -> None:
    if not threads:
        print("No threads." if state == "all" else f"No {state} threads.")
        return
    project = None
    for t in threads:
        name = (t.get("project") or {}).get("name")
        if name and name != project:
            if project is not None:
                print()
            print(paint(name, output.BOLD) + paint(f"  {(t.get('project') or {}).get('title') or ''}", output.DIM))
            project = name
        print("\n".join(thread_lines(t)))
        print()


def _thread_done(args, t: dict, verb: str) -> int:
    if args.json:
        print_json(t)
    else:
        print(f"{verb} {short(t.get('id'))} ({where(t)}), now {t.get('state')}.")
    return 0


# --- Commands ----------------------------------------------------------------------------


def cmd_reviews(args) -> int:
    with cli.api() as a:
        reviews = a.get(project_path(cli.project_ref(args.project)) + "/reviews")
    if args.json:
        print_json(reviews)
        return 0
    if not reviews:
        print(f"No reviews of {args.project} yet. Start one with meertex review {args.project} start.")
        return 0
    rows = []
    for r in reviews:
        counts = r.get("counts") or {}
        on = (r.get("version") or {}).get("tag") or short(r.get("commit"))
        verdict = (r.get("verdict") or "").replace("_", " ")
        rows.append([short(r.get("id")), r.get("state") or "", verdict, who(r.get("reviewer")), on,
                     str(counts.get("open", 0)), str(counts.get("resolved", 0)), str(counts.get("accepted", 0)),
                     output.ago(r.get("submitted_at")) if r.get("submitted_at") else ""])
    print(table(rows, ["ID", "STATE", "VERDICT", "REVIEWER", "ON", "OPEN", "RESOLVED", "ACCEPTED", "SUBMITTED"]))
    for r in reviews:
        if r.get("summary"):
            print(f"\n{paint(short(r.get('id')), output.BOLD)} {who(r.get('reviewer'))}")
            print("\n".join(f"  {line}".rstrip() for line in r["summary"].split("\n")))
    return 0


def cmd_threads(args) -> int:
    params: dict[str, Any] = {"state": args.state}
    with cli.api() as a:
        if args.project:
            if args.review:
                params["review"] = args.review
            if args.path:
                params["path"] = args.path
            threads = a.get(project_path(cli.project_ref(args.project)) + "/threads", params=params)
        else:
            if args.review or args.path:
                raise cli.UsageError("--review and --path need a project.")
            threads = a.get("/api/threads", params=params)
    if args.json:
        print_json(threads)
    else:
        print_threads(threads, args.state)
    return 0


def cmd_thread(args) -> int:
    with cli.api() as a:
        t = a.get(thread_url(cli.project_ref(args.project), args.id))
    if args.json:
        print_json(t)
    else:
        print("\n".join(thread_lines(t)))
    return 0


def cmd_reply(args) -> int:
    stdin = _Stdin()
    body: dict[str, Any] = {"body": stdin(args.text) or ""}
    if args.suggestion is not None:
        body["suggestion"] = stdin(args.suggestion)
    with cli.api() as a:
        t = a.post(thread_url(cli.project_ref(args.project), args.id) + "/comments", body)
    return _thread_done(args, t, "Replied to")


def _note(args) -> dict:
    note = _Stdin()(args.note)
    return {"note": note} if note else {}


def cmd_resolve(args) -> int:
    with cli.api() as a:
        t = a.post(thread_url(cli.project_ref(args.project), args.id) + "/resolve", _note(args))
    return _thread_done(args, t, "Resolved")


def cmd_accept(args) -> int:
    with cli.api() as a:
        t = a.post(thread_url(cli.project_ref(args.project), args.id) + "/accept", _note(args))
    return _thread_done(args, t, "Accepted")


def cmd_reopen(args) -> int:
    with cli.api() as a:
        t = a.post(thread_url(cli.project_ref(args.project), args.id) + "/reopen", _note(args))
    return _thread_done(args, t, "Reopened")


def cmd_apply(args) -> int:
    body = {"comment_id": args.comment} if args.comment else {}
    with cli.api() as a:
        t = a.post(thread_url(cli.project_ref(args.project), args.id) + "/apply", body)
    if args.json:
        print_json(t)
    else:
        print(f"Applied the suggestion to {where(t)} in the live text and resolved {short(t.get('id'))}.")
    return 0


def cmd_comment(args) -> int:
    ref = cli.project_ref(args.project)
    path, first, last = parse_lines(args.place)
    stdin = _Stdin()
    body: dict[str, Any] = {"path": path, "ref": "live", "line_start": first, "line_end": last,
                            "body": stdin(args.text) or ""}
    if args.suggestion is not None:
        body["suggestion"] = stdin(args.suggestion)
    with cli.api() as a:
        # The lines are the server's live text, which a checkout that is not
        # in sync may number differently. Checking them first gives a clear
        # error and shows what the comment is about.
        resp = a.get(f"{project_path(ref)}/files/{file_path(path)}", params={"ref": "live"}, raw=True)
        count = line_count(resp.text)
        if last > count:
            raise cli.UsageError(f"{path} has {output.count(count, 'line')} in the live text, "
                                 f"so {lines_label(first, last)} is not there.")
        started = None
        if args.review:
            review = pending_review(a, ref)
            if review is None:
                review = started = a.post(project_path(ref) + "/reviews", {"ref": "HEAD"})
            body["review_id"] = review["id"]
        t = a.post(project_path(ref) + "/threads", body)
    if args.json:
        print_json(t)
        return 0
    if started:
        print(f"Started a review on HEAD (review {short(started.get('id'))}).")
    pending = " to your pending review" if t.get("pending") else ""
    print(f"Added thread {short(t.get('id'))} on {where(t)}{pending}.")
    print("\n".join(_block(t.get("quote") or "", "  ")))
    if t.get("pending"):
        print(paint(f"Nobody sees it until you submit with meertex review {args.project} submit --verdict V.",
                    output.DIM))
    return 0


def cmd_review(args) -> int:
    ref = cli.project_ref(args.project)
    with cli.api() as a:
        if args.action == "start":
            resp = a.request("POST", project_path(ref) + "/reviews", json={"ref": args.ref or "HEAD"}, raw=True)
            review, created = resp.json(), resp.status_code == 201
        else:
            review = pending_review(a, ref)
            if review is None:
                raise cli.UsageError(f"You have no pending review of {args.project}. "
                                     f"Start one with meertex review {args.project} start.")
            url = f"{project_path(ref)}/reviews/{quote(review['id'], safe='')}"
            if args.action == "submit":
                summary = _Stdin()(args.summary)
                review = a.post(url + "/submit", {"verdict": args.verdict, "summary": summary or ""})
            else:
                a.delete(url)
    if args.json:
        print_json({"discarded": review["id"]} if args.action == "discard" else review)
        return 0
    rid = short(review.get("id"))
    on = (review.get("version") or {}).get("tag") or short(review.get("commit"))
    count = sum((review.get("counts") or {}).values())
    if args.action == "start":
        lead = "Started review" if created else "You have a pending review already,"
        print(f"{lead} {rid} on {on}.")
        print(paint(f"Comment with meertex comment {args.project} PATH:LINE TEXT --review, "
                    f"then meertex review {args.project} submit --verdict V.", output.DIM))
    elif args.action == "submit":
        verdict = (review.get("verdict") or "").replace("_", " ")
        print(f"Submitted review {rid} ({verdict}) with {output.count(count, 'thread')}. "
              "The owner and the editors get a mail.")
    else:
        print(f"Discarded review {rid} and its {output.count(count, 'thread')}.")
    return 0


# --- Parser ------------------------------------------------------------------------------


def _verdict(value: str) -> str:
    value = value.strip().lower().replace("-", "_").replace(" ", "_")
    if value not in VERDICTS:
        raise argparse.ArgumentTypeError(f"choose from {', '.join(VERDICTS)}")
    return value


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    """Register the commands with cli.py's ``cmd`` helper. ``common`` carries
    ``--json`` for the subcommands of ``review``."""
    p = cmd("reviews", cmd_reviews, "A project's reviews")
    p.add_argument("project")

    p = cmd("threads", cmd_threads, "Review threads with their text and conversation (all projects without one)")
    p.add_argument("project", nargs="?")
    p.add_argument("--state", choices=STATES, default="open")
    p.add_argument("--review", metavar="ID", help="only this review's threads")
    p.add_argument("--path", help="only the threads on this file")

    p = cmd("thread", cmd_thread, "One review thread")
    p.add_argument("project")
    p.add_argument("id", help="the thread's id, or its first 8 characters")

    p = cmd("reply", cmd_reply, "Answer in a thread")
    p.add_argument("project")
    p.add_argument("id")
    p.add_argument("text", metavar="TEXT", help="the reply, - to read it from stdin")
    p.add_argument("--suggestion", metavar="TEXT", help="replacement text for the thread's span")

    for name, func, text in (
        ("resolve", cmd_resolve, "Mark a thread as dealt with (editors and the owner)"),
        ("accept", cmd_accept, "Accept how a thread was dealt with (who started it, or the owner)"),
        ("reopen", cmd_reopen, "Open a resolved or accepted thread again"),
    ):
        p = cmd(name, func, text)
        p.add_argument("project")
        p.add_argument("id")
        p.add_argument("--note", metavar="TEXT", help="a note that goes with it")

    p = cmd("apply", cmd_apply, "Write a thread's suggestion into the live text and resolve it")
    p.add_argument("project")
    p.add_argument("id")
    p.add_argument("--comment", metavar="ID", help="whose suggestion, default the newest")

    p = cmd("comment", cmd_comment, "Comment on lines of the live text")
    p.add_argument("project")
    p.add_argument("place", metavar="PATH:LINE[-LINE]")
    p.add_argument("text", metavar="TEXT", help="the comment, - to read it from stdin")
    p.add_argument("--suggestion", metavar="TEXT", help="replacement text for those lines")
    p.add_argument("--review", action="store_true", help="add it to your pending review, starting one on HEAD")

    p = cmd("review", cmd_review, "Start, submit or discard your review of a project")
    p.add_argument("project")
    rsub = p.add_subparsers(dest="action", metavar="start|submit|discard", required=True)
    r = rsub.add_parser("start", parents=[common], help="start a review on HEAD, a version's tag, a commit or live")
    r.add_argument("--ref", help="what to review, default HEAD")
    r = rsub.add_parser("submit", parents=[common], help="submit your pending review with a verdict")
    r.add_argument("--verdict", type=_verdict, required=True, metavar="|".join(VERDICTS))
    r.add_argument("--summary", metavar="TEXT", help="the review's summary, - to read it from stdin")
    rsub.add_parser("discard", parents=[common], help="throw your pending review away with its threads")
