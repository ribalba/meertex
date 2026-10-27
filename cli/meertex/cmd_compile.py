"""``meertex compile`` (docs/DESIGN.md §14).

Compiles a project on the server, prints the status and the problems (errors,
warnings and bad boxes with ``path:line``), and saves the PDF. An agent that
edits a paper runs it to see whether the paper still compiles, without TeX on
its own machine.

Without ``--out`` the PDF of a checkout goes to ``<main file stem>.pdf`` at
the checkout's root, which the sync client never sends (§10, ignored files),
so compiling never adds a file to the paper. Otherwise it goes to the current
folder.

The command lives here rather than in cli.py, which only registers it, and
reaches the API through ``cli.api()`` at call time, so the tests' fake API
applies to it as well.
"""

import argparse
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from . import cli, config, output
from .api import project_path
from .output import paint, print_json

# A compile waits for TeX, behind other compiles of the same project too.
COMPILE_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
LEVEL_COLORS = {"error": output.RED, "warning": output.AMBER}
STATUS_COLORS = {"success": output.GREEN, "errors": output.AMBER}


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    p = cmd(
        "compile",
        cmd_compile,
        "Compile a project on the server, print its problems and save the PDF",
        epilog="Exits 0 when the compile succeeded and 1 when it had errors or made no PDF.",
    )
    p.add_argument("project", help="name, id, or a checkout folder such as '.'")
    p.add_argument("--ref", default="live", help="live (the default), HEAD, a version's tag or a commit")
    p.add_argument(
        "--out",
        help="where the PDF goes. By default <main file>.pdf at the root of a checkout, else <project>.pdf here.",
    )


def _is_path(value: str) -> bool:
    return value == "." or "/" in value or value.startswith("~")


def default_out(project: str, ref: str, main_file: str) -> Path:
    if _is_path(project):
        root = config.find_checkout(Path(project).expanduser())
        if root is not None:
            return root / f"{PurePosixPath(main_file).stem}.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", project).strip("-") or "paper"
    suffix = "" if ref == "live" else "-" + re.sub(r"[^A-Za-z0-9._-]+", "-", ref)
    return Path.cwd() / f"{name}{suffix}.pdf"


def _problem_line(p: dict) -> str:
    level = p.get("level") or "?"
    where = f"{p['path']}:{p['line']}" if p.get("path") and p.get("line") else (p.get("path") or "")
    label = paint(f"{level:<8}", LEVEL_COLORS.get(level))
    return f"  {label} {where + '  ' if where else ''}{p.get('message', '')}"


def cmd_compile(args: argparse.Namespace) -> int:
    ref = cli.project_ref(args.project)
    with cli.api() as a:
        result = a.post(project_path(ref) + "/compile", {"ref": args.ref}, timeout=COMPILE_TIMEOUT)
        pdf = None
        if result.get("pdf_url"):
            pdf = a.request("GET", result["pdf_url"], raw=True, timeout=COMPILE_TIMEOUT).content
    saved = None
    if pdf is not None:
        saved = Path(args.out).expanduser() if args.out else default_out(args.project, args.ref, result["main_file"])
        saved.parent.mkdir(parents=True, exist_ok=True)
        config.write_atomic(saved, pdf)

    status = result.get("status", "")
    if args.json:
        print_json({**result, "saved": str(saved) if saved else None})
    else:
        seconds = (result.get("duration_ms") or 0) / 1000
        print(
            f"{paint(status, STATUS_COLORS.get(status, output.RED))}  {result.get('ref')}  "
            f"{result.get('compiler')}  {result.get('main_file')}  {seconds:.1f} s"
        )
        for problem in result.get("problems") or []:
            print(_problem_line(problem))
        if saved is not None:
            print(f"Saved {saved}")
        else:
            cli.note("No PDF. The log is at " + (config.server() or "").rstrip("/") + result.get("log_url", ""))
    return 0 if status == "success" else 1
