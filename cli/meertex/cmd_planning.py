"""Plans and the timeline from the command line (docs/DESIGN.md §3, §4, §5).

`meertex start` turns a plan ("something for ICT4S") into a paper, and
`meertex timeline` prints what the web app's Gantt chart shows: when to start
writing which paper, what is due when, and where writing windows overlap.
"""

import argparse
import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from . import cli, output
from .api import project_path
from .output import paint, print_json, table

# Dates the timeline lists, from now on.
HORIZON_DAYS = 400


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _seconds_left(when: datetime, now: datetime) -> float:
    return (when - now).total_seconds()


def _day(value: datetime) -> str:
    return value.strftime("%Y-%m-%d")


def cmd_start(args) -> int:
    body = {"title": args.title, "starter": args.starter, "keep_name": args.keep_name}
    if args.paper:
        body["paper"] = args.paper
    with cli.api() as a:
        project = a.post(project_path(cli.project_ref(args.project)) + "/start", body)
    sync_result = None
    if args.checkout is not None:
        root = cli.target_dir(project["name"], args.checkout or None)
        sync_result = cli.start_checkout(root, project, cli.config.server(), cli.sync_log(args, quiet=True))
        project = {**project, "checkout_dir": str(root)}
    if args.json:
        print_json(project)
    else:
        print(f"Started {paint(project['name'], output.BOLD)}: {project.get('title')}")
        if project.get("url"):
            print(f"  url       {project['url']}")
        if sync_result is not None:
            cli.print_checkout(cli.Path(project["checkout_dir"]), sync_result)
    return 0 if sync_result is None or sync_result.get("ok") else 1


def _windows(projects: list[dict]) -> list[tuple[datetime, datetime, dict]]:
    out = []
    for p in projects:
        w = p.get("writing")
        start, end = _instant((w or {}).get("start")), _instant((w or {}).get("end"))
        if start and end:
            out.append((start, end, p))
    return out


def _overlaps(windows: list[tuple[datetime, datetime, dict]], now: datetime) -> list[tuple[datetime, datetime, list[str]]]:
    """Stretches of time, from now on, in which two or more writing windows run."""
    edges = sorted({max(t, now) for start, end, _p in windows for t in (start, end) if end > now})
    out: list[tuple[datetime, datetime, list[str]]] = []
    for a, b in itertools.pairwise(edges):
        names = sorted(p["name"] for start, end, p in windows if start < b and end > a)
        if len(names) < 2:
            continue
        if out and out[-1][2] == names and out[-1][1] == a:
            out[-1] = (out[-1][0], b, names)
        else:
            out.append((a, b, names))
    return out


def cmd_timeline(args) -> int:
    with cli.api() as a:
        if args.writing_days is not None:
            a.patch("/api/auth/me", {"writing_days": args.writing_days})
        data = a.get("/api/timeline", params={"archived": "all" if args.all else "false"})
    if args.json:
        print_json(data)
        return 0
    projects = data.get("projects") or []
    now = _instant(data.get("today")) or datetime.now(UTC)
    print(f"Writing a paper takes you {data.get('writing_days')} days. "
          f"meertex timeline --writing-days N changes that.")
    if not projects:
        print("\nNo papers yet. meertex new, or meertex new --planned for a venue without a topic yet.")
        return 0

    windows = _windows(projects)
    rows = []
    for start, end, p in sorted(windows, key=lambda w: w[0]):
        if end < now:
            continue
        name = p["name"] + (" (planned)" if p.get("planned") else "")
        w = p["writing"]
        own = "" if w.get("own") else ", your usual time"
        # The venue's own day. 23:59 AoE is already the next day in UTC.
        due = next((d for d in p.get("dates") or [] if d.get("id") == w.get("date_id")), None)
        due_day = datetime.fromisoformat(due["local"]).date() if due and due.get("local") else end.date()
        start_day = due_day - timedelta(days=w["days"])
        what = f"{'writing now' if start <= now else 'start writing'}, due {output.when(due) or due_day} ({w['days']} days{own})"
        when = paint("now", output.BOLD) if start <= now else output.countdown(_seconds_left(start, now))
        rows.append([start_day.isoformat(), when, what, name])
    if rows:
        print("\nWriting")
        print(table(rows, ["START", "", "WHAT", "PAPER"], indent="  "))

    dates = []
    horizon = now + timedelta(days=HORIZON_DAYS)
    for p in projects:
        for d in p.get("dates") or []:
            due = _instant(d.get("due_at"))
            if due and now <= due <= horizon and d.get("answer") is None:
                dates.append((due, d, p))
    if dates:
        print("\nDates")
        print(table(
            [[output.when(d), output.countdown(d.get("seconds_left")), d.get("label") or d.get("kind"),
              p["name"] + (" (planned)" if p.get("planned") else "")]
             for _due, d, p in sorted(dates, key=lambda x: x[0])],
            ["DUE", "", "WHAT", "PAPER"], indent="  "))

    overlaps = _overlaps(windows, now)
    if overlaps:
        print("\nOverlapping writing")
        for a, b, names in overlaps:
            colour = output.RED if len(names) >= 3 else output.AMBER
            print("  " + paint(f"{_day(a)} to {_day(b)}", colour) + f"  {len(names)} papers: {', '.join(names)}")
    return 0


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    p = cmd("start", cmd_start, "Turn a plan into a paper once its topic is clear")
    p.add_argument("project")
    p.add_argument("--title", required=True)
    p.add_argument("--paper", help="the short name, the {paper} of the naming pattern")
    p.add_argument("--starter", choices=("empty", "basic"), default="empty",
                   help="starting files when the project has none, empty (default) or basic")
    p.add_argument("--keep-name", action="store_true", help="keep the plan's name instead of building it again")
    p.add_argument("--checkout", nargs="?", const="", metavar="DIR",
                   help="check it out, into DIR or $MEERTEX_PAPERS/<name>")

    p = cmd("timeline", cmd_timeline, "When to start writing which paper, what is due, and where it piles up")
    p.add_argument("--writing-days", type=int, metavar="N", help="set how many days writing a paper takes you first")
    p.add_argument("--all", action="store_true", help="archived papers too")
