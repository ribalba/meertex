"""meertex update, and the daily look for a newer meertex on PyPI
(docs/DESIGN.md §12, Updating).

Once a day, after a command that a person ran in a terminal, the CLI asks
PyPI for the newest version. When there is a newer one it offers to update.
The update runs the way meertex was installed (pipx, uv tool, pip in a venv
or pip --user) and then restarts the sync service, so the background sync
runs the new code too. Scripts and agents never get the question. It only
comes when stdin, stdout and stderr are all a terminal and the command did not
ask for --json.

An install from a folder or a URL (``pipx install ./cli``, ``pip install -e``)
is left alone, because its version says nothing about PyPI's.
"""

import argparse
import json
import os
import shlex
import shutil
import site
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path

import httpx

from . import __version__, cli, config, output, service
from .output import paint, print_json

PYPI_URL = "https://pypi.org/pypi/meertex/json"
STATE_FILE = "update.json"
CHECK_EVERY = timedelta(days=1)
TIMEOUT = 3.0


def parse_version(value: str) -> tuple[int, ...] | None:
    """``0.1.0.42`` as numbers. None for anything else, such as a pre-release."""
    parts = value.strip().split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def newer(latest: str | None, current: str) -> bool:
    a, b = parse_version(latest or ""), parse_version(current)
    return a is not None and b is not None and a > b


def fetch_latest(transport: httpx.BaseTransport | None = None) -> str:
    """The newest release on PyPI. Raises httpx.HTTPError or ValueError."""
    with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=True,
                      headers={"User-Agent": f"meertex/{__version__}"}) as client:
        res = client.get(PYPI_URL)
        res.raise_for_status()
        data = res.json()
    info = data.get("info") if isinstance(data, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or parse_version(version) is None:
        raise ValueError(f"PyPI answered without a usable version ({version!r}).")
    return version


def install() -> dict:
    """How this meertex was installed. ``how`` is pipx, uv, venv, user,
    system or source. ``command`` updates it, and is None when meertex cannot
    update itself. ``hint`` then says what to do instead."""
    here = Path(__file__).resolve().parent
    try:
        dist = metadata.distribution("meertex")
        location = Path(dist.locate_file("")).resolve()
    except metadata.PackageNotFoundError:
        dist = None
    # Without a RECORD it is the egg-info of a source tree, not an install.
    if dist is None or dist.read_text("RECORD") is None or not here.is_relative_to(location):
        return {"how": "source", "from": str(here.parent), "command": None,
                "hint": "It runs from a source folder. Update it there."}
    direct = dist.read_text("direct_url.json")
    if direct:
        try:
            url = json.loads(direct).get("url")
        except (ValueError, AttributeError):
            url = None
        return {"how": "source", "from": url or str(location), "command": None,
                "hint": f"It was installed from {url or location}. Update it from there."}

    prefix = Path(sys.prefix)
    # pip says only what goes wrong. "Updated to ..." comes afterwards.
    pip = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade"]
    if (prefix / "pipx_metadata.json").is_file():
        how, command = "pipx", ["pipx", "upgrade", "meertex"]
    elif (prefix / "uv-receipt.toml").is_file():
        how, command = "uv", ["uv", "tool", "upgrade", "meertex"]
    elif sys.prefix != sys.base_prefix:
        how, command = "venv", [*pip, "meertex"]
    elif location == Path(site.getusersitepackages()).resolve():
        how, command = "user", [*pip, "--user", "meertex"]
    else:
        return {"how": "system", "from": str(location), "command": None,
                "hint": f"It is installed for all users in {location}. Update it the way it was installed."}
    if shutil.which(command[0]) is None:
        return {"how": how, "from": str(location), "command": None,
                "hint": f"{command[0]} is not on PATH. Update it with {shlex.join(command)}."}
    return {"how": how, "from": str(location), "command": command, "hint": None}


def installed_version() -> str | None:
    """The version on disk after an update. A new Python has to tell, because
    this one still has the old code loaded. -P keeps the current folder off
    sys.path, so a source tree there does not answer instead."""
    code = "from importlib.metadata import version; print(version('meertex'))"
    try:
        done = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return (done.stdout.strip() or None) if done.returncode == 0 else None


# --- The state of the daily look ------------------------------------------------------


def read_state() -> dict:
    try:
        value = config.read_json(config.home() / STATE_FILE, {})
    except config.ConfigError:
        return {}
    return value if isinstance(value, dict) else {}


def write_state(**values) -> None:
    try:
        config.write_json(config.home() / STATE_FILE, {**read_state(), **values})
    except OSError:
        pass


def due(state: dict, now: datetime) -> bool:
    checked = output.parse_time(state.get("checked_at"))
    return checked is None or not checked <= now < checked + CHECK_EVERY


# --- Updating -----------------------------------------------------------------------------


def apply(how: dict, latest: str, as_json: bool = False) -> dict:
    """Run the update, then restart the sync service when it runs and the
    version changed. Returns what happened."""
    command = how["command"]
    result = {"updated": False, "version": __version__, "service_restarted": False, "error": None}
    cli.note(paint(f"Running {shlex.join(command)}", output.DIM, sys.stderr))
    try:
        # With --json, stdout is for the JSON. The update's own output goes to stderr (fd 2).
        done = subprocess.run(command, stdout=2 if as_json else None, check=False)
    except OSError as exc:
        result["error"] = f"{command[0]} could not run ({exc})."
        return result
    except KeyboardInterrupt:
        result["error"] = "The update was stopped."
        return result
    if done.returncode:
        result["error"] = f"The update failed with exit status {done.returncode}."
        return result
    version = installed_version() or latest
    result["version"] = version
    result["updated"] = version != __version__
    if version != latest:
        result["error"] = f"The update ran, but meertex {version} is installed and not {latest}."
    if result["updated"]:
        try:
            result["service_restarted"] = service.restart()
        except (service.Unavailable, OSError, subprocess.CalledProcessError) as exc:
            result["error"] = (f"{service.name()} still runs meertex {__version__} ({exc}). "
                               f"Restart it with {service.restart_command()}.")
    return result


def report(result: dict) -> None:
    if result["updated"]:
        print(f"Updated to meertex {result['version']}.")
    if result["service_restarted"]:
        print(f"Restarted {service.name()}, so the background sync runs it too.")
    if result["error"]:
        cli.err(result["error"])


def _terminal() -> bool:
    return all(s is not None and s.isatty() for s in (sys.stdin, sys.stdout, sys.stderr))


def wanted(args, code: int) -> bool:
    """Whether to look for a newer version after this command."""
    if code == 130 or getattr(args, "json", False) or args.command == "update":
        return False
    # meertex sync without --once runs until Ctrl-C, and the question would come after it.
    if args.command == "sync" and not args.once:
        return False
    if os.environ.get("MEERTEX_NO_UPDATE_CHECK"):
        return False
    try:
        if config.read_config().get("update_check") is False:
            return False
    except config.ConfigError:
        return False
    return _terminal()


def ask(question: str) -> bool:
    print(question, end="", file=sys.stderr, flush=True)
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return False
    return bool(line) and line.strip().lower() in ("", "y", "yes")


def offer(args, code: int) -> None:
    """After a command, at most once a day: look for a newer meertex on PyPI
    and offer to update. Any trouble on the way is silent, the command's own
    result is what counts."""
    if not wanted(args, code):
        return
    now = datetime.now(UTC)
    if not due(read_state(), now):
        return
    how = install()
    if how["how"] == "source":
        return
    try:
        latest = fetch_latest()
    except (httpx.HTTPError, ValueError, KeyboardInterrupt):
        latest = None
    # Also after a failed look, so a network that is down does not cost every command the timeout.
    write_state(checked_at=now.isoformat(timespec="seconds"), **({"latest": latest} if latest else {}))
    if not newer(latest, __version__):
        return
    head = f"meertex {latest} is out, you have {__version__}."
    if how["command"] is None:
        cli.note("\n" + paint(f"{head} {how['hint']}", output.DIM, sys.stderr))
        return
    if not ask(f"\n{head} Update now? [Y/n] "):
        cli.note(paint("Update later with meertex update. meertex asks again tomorrow.", output.DIM, sys.stderr))
        return
    report(apply(how, latest))


# --- meertex update -----------------------------------------------------------------------


def cmd_update(args) -> int:
    how = install()
    result = {"installed": __version__, "latest": None, "newer": False, "how": how["how"],
              "from": how["from"], "command": how["command"]}
    if how["how"] != "source":
        try:
            result["latest"] = fetch_latest()
        except (httpx.HTTPError, ValueError) as exc:
            raise cli.UsageError(f"PyPI did not tell the newest version ({exc}).") from exc
        write_state(checked_at=datetime.now(UTC).isoformat(timespec="seconds"), latest=result["latest"])
        result["newer"] = newer(result["latest"], __version__)

    if not result["newer"] or args.check or how["command"] is None:
        if args.json:
            print_json(result)
        elif how["how"] == "source":
            print(f"This is meertex {__version__}. {how['hint']}")
        elif not result["newer"]:
            print(f"meertex {__version__} is the newest version.")
        else:
            follow = "Update with meertex update." if how["command"] else how["hint"]
            print(f"meertex {result['latest']} is out, you have {__version__}. {follow}")
        return 0

    done = apply(how, result["latest"], as_json=args.json)
    if args.json:
        print_json({**result, **done})
    else:
        report(done)
    return 0 if done["updated"] else 1


def add_commands(cmd: Callable[..., argparse.ArgumentParser], common: argparse.ArgumentParser) -> None:
    p = cmd("update", cmd_update, "Update meertex from PyPI the way it was installed, and restart the sync service")
    p.add_argument("--check", action="store_true", help="only say whether a newer version is out")
