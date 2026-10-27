"""meertex reminders prints what the next reminder mail would say
(docs/DESIGN.md §5, Reminders).

It prints GET /api/reminders/preview. That leaves out what earlier mails said
already, so it answers "what is coming up" at any time of the day: the dates
within your reminder days, when to start writing, and the questions waiting.
"""

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import cli, output
from .output import paint, print_json


def days_text(days: list[int]) -> str:
    """``14, 3 and 1 days before and on the day``."""
    before = [str(d) for d in days if d > 0]
    parts = []
    if before:
        joined = before[0] if len(before) == 1 else ", ".join(before[:-1]) + " and " + before[-1]
        parts.append(f"{joined} {'day' if before == ['1'] else 'days'} before")
    if 0 in days:
        parts.append("on the day")
    return " and ".join(parts) or "no days before a date"


def _paper_head(p: dict) -> str:
    venue = output.venue(p)
    name = paint(p["name"], output.BOLD)
    if p.get("planned"):
        return f"{name}  {venue + ', ' if venue else ''}still a plan"
    title = output.truncate(p.get("title") or "", 70)
    return "  ".join(x for x in (name, title, f"({venue})" if venue else "") if x)


def _marker(item: dict) -> str:
    if item["type"] == "question":
        return paint("question", output.AMBER)
    if item["type"] == "writing":
        return paint("writing ", output.CYAN)
    left = item.get("days_left", 99)
    colour = output.RED if left < 3 else output.AMBER if left < 14 else None
    return paint("date    ", colour)


def cmd_reminders(args) -> int:
    with cli.api() as a:
        data = a.get("/api/reminders/preview")
    if args.json:
        print_json(data)
        return 0

    zone = data.get("timezone") or "UTC"
    hour = f"{int(data.get('reminder_hour') or 0):02d}:00"
    if data.get("reminders"):
        print(f"Reminder mails are on, {days_text(data.get('reminder_days') or [])}, at {hour} {zone}.")
        nxt = output.parse_time(data.get("next_at"))
        if nxt is not None:
            try:
                here, shown = nxt.astimezone(ZoneInfo(zone)), zone
            except (ZoneInfoNotFoundError, ValueError):
                here, shown = nxt, "UTC"
            left = (nxt - datetime.now(UTC)).total_seconds()
            soon = "within a few minutes" if left < 600 else output.countdown(left)
            print(f"The next one is put together on {here.strftime('%Y-%m-%d at %H:%M')} {shown}, {soon}.")
    else:
        print("Reminder mails are off. This is what they would say.")
    if data.get("sending") is False:
        print(paint("This server does not send them (REMINDERS_ENABLED is off).", output.DIM))

    projects = data.get("projects") or []
    if not projects:
        print("\nNothing to remind you of right now.")
    for p in projects:
        print()
        print(_paper_head(p))
        for item in p.get("items") or []:
            print(f"  {_marker(item)}  {item.get('text')}")
        if p.get("url"):
            print(paint(f"  {p['url']}", output.DIM))

    muted = [m["name"] for m in data.get("muted") or []]
    if muted:
        print(paint(f"\nMuted: {', '.join(muted)}", output.DIM))
    print(paint(f"\nChange the days and the hour at {data.get('settings_url')}", output.DIM))
    return 0


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    cmd("reminders", cmd_reminders,
        "What your next reminder mail would say: dates coming up, when to start writing, waiting questions")
