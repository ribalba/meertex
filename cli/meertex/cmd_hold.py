"""``meertex hold`` and ``meertex release`` (docs/DESIGN.md §10, Holding).

A held checkout keeps its local changes to itself while other people's edits
keep arriving. It is meant for a large rewrite that coauthors should not see
half done. The state is the marker ``.meertex/hold``, so a running sync
service and the CLI agree on it without talking to each other. Release
removes the marker and syncs, through the service when one runs (its
``.meertex/request``) or right here like ``sync --once``.

The command lives here rather than in cli.py, which only registers it and
calls the small helpers below from ``sync`` and ``status``.
"""

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path

from . import cli, config, output
from .engine import Engine
from .output import paint, print_json
from .sync import sync_many_once


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    where = "checkout folders, default the checkout that contains the current folder"
    p = cmd("hold", cmd_hold, "Keep local changes local for now, while other people's edits still arrive",
            epilog="meertex release sends everything that was held in one go.")
    p.add_argument("dirs", nargs="*", metavar="DIR", help=where)
    p = cmd("release", cmd_release, "Send the held changes and sync as usual again")
    p.add_argument("dirs", nargs="*", metavar="DIR", help=where)


def roots_of(dirs: list[str]) -> list[Path]:
    """The checkouts named, or the one that contains the current folder."""
    if dirs:
        return cli._checkout_roots(dirs)
    root = config.find_checkout(Path.cwd())
    if root is None:
        raise cli.UsageError("This folder is not inside a meertex checkout. Name the checkout's folder.")
    return [root]


def held_files(root: Path) -> int:
    """How many files have local changes that the document does not have.
    It reads the saved state and the disk and changes neither, so it is safe
    while a sync service works on the same folder."""
    meta = config.read_project(root) or {}
    engine = Engine(root, main_file=meta.get("main_file") or "main.tex")
    engine.load()
    return len(engine.local_changes())


def hold_roots(dirs: list[str]) -> list[dict]:
    results = []
    for root in roots_of(dirs):
        since, new = config.set_hold(root)
        meta = config.read_project(root) or {}
        results.append({"dir": str(root), "name": meta.get("name") or root.name, "held_since": since,
                        "already": not new})
    return results


def cmd_hold(args) -> int:
    results = hold_roots(args.dirs)
    if args.json:
        print_json(results)
        return 0
    for r in results:
        name = paint(r["name"], output.BOLD)
        if r["already"]:
            print(f"{name} is held already, since {output.ago(r['held_since'])}.")
        else:
            print(f"Holding {name}. Your changes stay here until meertex release, "
                  "and other people's edits still arrive.")
    return 0


def cmd_release(args) -> int:
    roots = roots_of(args.dirs)
    token = config.token()
    states = []
    for root in roots:
        meta = config.read_project(root) or {}
        since = config.held_since(root)
        count = held_files(root) if since is not None else 0
        config.clear_hold(root)
        states.append({"dir": str(root), "name": meta.get("name") or root.name, "was_held": since is not None,
                       "held_since": since or None, "held_files": count})
    released = [Path(s["dir"]) for s in states if s["was_held"]]
    results = asyncio.run(sync_many_once(released, token, cli.sync_log(args, quiet=True))) if released else []
    by_dir = {r["dir"]: r for r in results}
    for s in states:
        r = by_dir.get(s["dir"], {})
        s.update(ok=r.get("ok", True), error=r.get("error"), via_service=bool(r.get("via_service")))
    if args.json:
        print_json(states)
    else:
        for s in states:
            name = paint(s["name"], output.BOLD)
            if not s["was_held"]:
                print(f"{name} was not held.")
            elif s["ok"]:
                print(f"Released {name}. {output.count(s['held_files'], 'held file')} went to the server.")
            else:
                print(f"Released {name}, but the sync did not finish ({s['error']}). "
                      "The held changes go out with the next sync.")
    return 0 if all(s["ok"] for s in states) else 1


# --- Hooks for cli.py ----------------------------------------------------------------


def hold_for_sync(args) -> list[str]:
    """``meertex sync --hold``: hold the given or current checkouts, and sync those."""
    results = hold_roots(args.dirs)
    if not args.json:
        for r in results:
            cli.note(f"Holding {r['name']}. Local changes stay here until meertex release.")
    return [r["dir"] for r in results]


def once_state(result: dict) -> str | None:
    """How ``sync --once`` reports a held checkout, which it only pulls."""
    if not result.get("held"):
        return None
    files = output.count(result.get("held_files") or 0, "file")
    return paint("pulled", output.GREEN) + f", held, so the changes to {files} stay here until meertex release"


def hold_state(root: Path, exists: bool) -> dict:
    """The fields ``meertex status`` adds for holding."""
    since = config.held_since(root) if exists else None
    return {"held": since is not None, "held_since": since or None,
            "held_files": held_files(root) if since is not None else None}


def print_held(states: list[dict]) -> None:
    for s in states:
        if s.get("held"):
            files = output.count(s.get("held_files") or 0, "file")
            since = f" since {output.ago(s['held_since'])}" if s.get("held_since") else ""
            print(paint(f"{s['name']}  held{since}, {files} with held changes. "
                        f"meertex release {s['dir']} sends them.", output.AMBER))
