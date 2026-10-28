"""The service that runs ``meertex sync`` in the background. On Linux it is a
systemd user unit, on macOS a launchd agent in ~/Library/LaunchAgents.

Both point at the ``meertex`` executable that installed them, so a pipx or
venv install keeps working without being on the service manager's PATH. The
variables that move the CLI's configuration (MEERTEX_HOME, MEERTEX_PAPERS,
MEERTEX_SERVER) are copied in, so the service syncs the same checkouts as the
shell that installed it. The token never goes into the unit or the plist.

``status`` reads the unit back from systemd and its log from the journal. On
macOS it asks launchctl, and the log is the agent's file in ~/Library/Logs.
"""

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.parsers.expat import ExpatError

# Read on every call, so the tests can try the other system.
MAC = sys.platform == "darwin"
UNIT_NAME = "meertex.service"
LABEL = "com.meertex.sync"
CARRIED_VARIABLES = ("MEERTEX_HOME", "MEERTEX_PAPERS", "MEERTEX_SERVER")
# What status asks systemctl for. Times come as UTC, so they parse the same
# in every time zone.
PROPERTIES = ("LoadState", "FragmentPath", "UnitFileState", "ActiveState", "SubState", "MainPID",
              "NRestarts", "Result", "ExecMainStatus", "ActiveEnterTimestamp", "InactiveEnterTimestamp")


class Unavailable(Exception):
    """systemctl, journalctl or launchctl is missing, or the user's service
    manager does not answer."""


def name() -> str:
    """What the service is called on this system."""
    return LABEL if MAC else UNIT_NAME


def start_command() -> str:
    """How the user starts the installed service by hand."""
    # A stopped agent may or may not still be loaded. install handles both.
    return "meertex service install" if MAC else f"systemctl --user start {UNIT_NAME}"


def load_command() -> str:
    """How the user starts the service that ``install --no-start`` wrote."""
    if MAC:
        return f"launchctl bootstrap {domain()} {shlex.quote(str(plist_path()))}"
    return f"systemctl --user daemon-reload && systemctl --user enable --now {UNIT_NAME}"


def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    return base / "systemd" / "user" / UNIT_NAME


def executable() -> list[str]:
    argv0 = Path(sys.argv[0])
    if argv0.name == "meertex" and argv0.exists():
        return [str(argv0.resolve())]
    found = shutil.which("meertex")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "meertex"]


def carried() -> dict[str, str]:
    """The CARRIED_VARIABLES that are set, with absolute paths."""
    values = {}
    for var in CARRIED_VARIABLES:
        value = os.environ.get(var)
        if value:
            if var != "MEERTEX_SERVER":
                value = str(Path(value).expanduser().resolve())
            values[var] = value
    return values


def _quote(arg: str) -> str:
    """systemd's quoting for ExecStart and Environment. Double quotes with
    backslash escapes, and only where needed."""
    if arg and not any(c in arg for c in ' \t"\\\'$%;'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def unit_text(command: list[str] | None = None) -> str:
    command = command or executable()
    lines = [
        "[Unit]",
        "Description=meertex sync client (keeps paper checkouts in step with the server)",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_quote(a) for a in [*command, "sync"]),
        "Restart=on-failure",
        "RestartSec=10",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    for var, value in carried().items():
        lines.append("Environment=" + _quote(f"{var}={value}"))
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def install(start: bool = True) -> dict:
    if MAC:
        return _agent_install(start)
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text())
    if start:
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", UNIT_NAME)
    return {"unit": str(path), "started": start}


def uninstall(stop: bool = True) -> dict:
    if MAC:
        return _agent_uninstall(stop)
    path = unit_path()
    if stop:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    if stop:
        _systemctl("daemon-reload")
    return {"unit": str(path), "removed": existed, "stopped": stop}


def restart_command() -> str:
    """How the user restarts the running service by hand."""
    return f"launchctl kickstart -k {domain()}/{LABEL}" if MAC else f"systemctl --user restart {UNIT_NAME}"


def restart() -> bool:
    """Restart the service when it runs, so it runs the meertex that is
    installed now (after ``meertex update``). Returns whether it ran. Raises
    Unavailable, OSError or CalledProcessError when the restart fails."""
    if not status(0)["running"]:
        return False
    if MAC:
        _launchctl("kickstart", "-k", f"{domain()}/{LABEL}")
    else:
        _systemctl("restart", UNIT_NAME)
    return True


def _cannot_run(argv: list[str], exc: OSError) -> Unavailable:
    return Unavailable(f"{argv[0]} could not run ({exc}). The service needs {'launchd' if MAC else 'systemd'}.")


def _call(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise _cannot_run(argv, exc) from exc


def _run(argv: list[str]) -> str:
    done = _call(argv)
    if done.returncode:
        said = done.stderr.strip() or f"exit status {done.returncode}"
        raise Unavailable(f"{' '.join(argv[:2])} failed. {said}")
    return done.stdout


def _utc(value: str | None) -> str | None:
    """``Tue 2026-10-27 22:14:14 UTC`` from ``--timestamp=utc`` as ISO 8601."""
    try:
        stamp = datetime.strptime(value.split(" ", 1)[1], "%Y-%m-%d %H:%M:%S UTC")
    except (AttributeError, IndexError, ValueError):
        return None
    return stamp.replace(tzinfo=UTC).isoformat()


def _exec_start(unit: Path) -> str | None:
    try:
        lines = unit.read_text().splitlines()
    except OSError:
        return None
    return next((line.split("=", 1)[1] for line in lines if line.startswith("ExecStart=")), None)


def status(lines: int = 20) -> dict:
    """What systemd (launchd on macOS) knows about the service, with the last
    ``lines`` lines of its log. A log that cannot be read leaves ``logs``
    empty and says why in ``logs_error``, so the service's state still shows."""
    if MAC:
        return _agent_status(lines)
    out = _run(["systemctl", "--user", "show", UNIT_NAME, "--timestamp=utc",
                *(f"--property={p}" for p in PROPERTIES)])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    unit = props.get("FragmentPath") or ""
    state = props.get("ActiveState") or "inactive"
    result = {
        "unit": unit or str(unit_path()),
        "installed": bool(unit),
        "enabled": props.get("UnitFileState") == "enabled",
        "running": state == "active",
        "state": state,
        "sub_state": props.get("SubState") or None,
        "since": _utc(props.get("ActiveEnterTimestamp" if state == "active" else "InactiveEnterTimestamp")),
        "pid": int(props.get("MainPID") or 0) or None,
        "restarts": int(props.get("NRestarts") or 0),
        "result": props.get("Result") or None,
        "exit_status": int(props.get("ExecMainStatus") or 0),
        "command": _exec_start(Path(unit)) if unit else None,
        "log_file": None,
        "logs": [],
        "logs_error": None,
    }
    return _with_logs(result, lines)


def _with_logs(result: dict, lines: int) -> dict:
    if lines:
        try:
            result["logs"] = logs(lines)
        except Unavailable as exc:
            result["logs_error"] = str(exc)
    return result


def journal_command(lines: int, follow: bool = False) -> list[str]:
    argv = ["journalctl", "--user", f"--unit={UNIT_NAME}", f"--lines={lines}", "--output=json", "--no-pager"]
    return [*argv, "--follow"] if follow else argv


def log_entry(line: str) -> dict | None:
    """One line of ``journalctl --output=json`` as ``{at, pid, priority, message}``."""
    try:
        entry = json.loads(line)
        at = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000, UTC)
    except (ValueError, KeyError, TypeError):
        return None
    message = entry.get("MESSAGE") or ""
    # journald hands over a message with control characters (colour) as bytes.
    if isinstance(message, list):
        message = bytes(message).decode("utf-8", "replace")
    pid = entry.get("_PID")
    return {"at": at.isoformat(), "pid": int(pid) if pid else None,
            "priority": int(entry.get("PRIORITY") or 6), "message": message}


def logs(lines: int) -> list[dict]:
    if MAC:
        return _agent_logs(lines)
    return [e for e in map(log_entry, _run(journal_command(lines)).splitlines()) if e]


def follow(lines: int) -> Iterator[dict]:
    """The last ``lines`` log lines, then every new one as it is written."""
    argv = tail_command(lines, follow=True) if MAC else journal_command(lines, follow=True)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    except OSError as exc:
        raise _cannot_run(argv, exc) from exc
    entries = file_entries(proc.stdout) if MAC else filter(None, map(log_entry, proc.stdout))
    try:
        yield from entries
    finally:
        proc.terminate()
        proc.wait()


# --- launchd (macOS) -----------------------------------------------------------------
#
# An agent has no journal. Its output goes to a file, and the sync client
# stamps the lines of a file with the day and the time zone (sync.Log), which
# is where the log's times come from.

STAMPED = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d) (.*)")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "meertex" / "sync.log"


def domain() -> str:
    """The user's login session, where the agents of ~/Library/LaunchAgents run."""
    return f"gui/{os.getuid()}"


def agent_plist(command: list[str] | None = None) -> bytes:
    log = str(log_path())
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [*(command or executable()), "sync"],
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **carried()},
        "RunAtLoad": True,
        # Restart=on-failure: a crash starts it again, a clean stop does not.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        # Stopping waits up to 20 seconds for the checkouts (sync.run_daemon).
        # launchd's own default would kill it at 20.
        "ExitTimeOut": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _launchctl(*args: str) -> None:
    subprocess.run(["launchctl", *args], check=True)


def _loaded() -> bool:
    done = subprocess.run(["launchctl", "print", f"{domain()}/{LABEL}"], capture_output=True, check=False)
    return done.returncode == 0


def _agent_install(start: bool) -> dict:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(agent_plist())
    if start:
        target = f"{domain()}/{LABEL}"
        # A loaded agent keeps its old plist, and bootstrap refuses a label
        # that is still loaded. So the old one has to be gone first.
        if _loaded():
            subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
            deadline = time.monotonic() + 40
            while _loaded() and time.monotonic() < deadline:
                time.sleep(0.2)
        _launchctl("enable", target)
        _launchctl("bootstrap", domain(), str(path))
    return {"unit": str(path), "started": start}


def _agent_uninstall(stop: bool) -> dict:
    path = plist_path()
    if stop:
        # An agent that is not loaded is fine here, so launchctl's complaint stays quiet.
        subprocess.run(["launchctl", "bootout", f"{domain()}/{LABEL}"], capture_output=True, check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"unit": str(path), "removed": existed, "stopped": stop}


def _print_fields(text: str) -> dict[str, str]:
    """The ``key = value`` lines of ``launchctl print`` that describe the
    service itself. Nested blocks (arguments, environment, ...) are skipped."""
    fields: dict[str, str] = {}
    depth = 0
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("{"):
            depth += 1
        elif line.startswith("}"):
            depth -= 1
        elif depth == 1 and " = " in line:
            key, value = line.split(" = ", 1)
            fields[key] = value
    return fields


def _disabled() -> bool:
    """Whether ``launchctl disable`` keeps the agent from loading at login."""
    try:
        out = _run(["launchctl", "print-disabled", domain()])
    except Unavailable:
        return False
    for line in out.splitlines():
        if line.strip().startswith(f'"{LABEL}"'):
            # Newer macOS says disabled or enabled, older true or false.
            return line.rsplit("=>", 1)[-1].strip() in ("disabled", "true")
    return False


def _started(pid: int) -> str | None:
    """When ``pid`` started, from how long ps says it runs (``[[dd-]hh:]mm:ss``)."""
    try:
        days, _, clock = _run(["ps", "-o", "etime=", "-p", str(pid)]).strip().rpartition("-")
        seconds = 0
        for part in clock.split(":"):
            seconds = seconds * 60 + int(part)
        seconds += int(days or 0) * 86400
    except (Unavailable, ValueError):
        return None
    return (datetime.now(UTC) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _program(plist: Path) -> str | None:
    try:
        return shlex.join(plistlib.loads(plist.read_bytes())["ProgramArguments"])
    except (OSError, ValueError, KeyError, TypeError, ExpatError):
        return None


def _agent_status(lines: int) -> dict:
    path = plist_path()
    done = _call(["launchctl", "print", f"{domain()}/{LABEL}"])
    # An agent that is not loaded is an exit status (113, could not find service).
    fields = _print_fields(done.stdout) if done.returncode == 0 else {}
    pid = int(fields.get("pid") or 0) or None
    code = re.match(r"\d+", fields.get("last exit code", ""))
    signal = re.search(r"\d+$", fields.get("last terminating signal", ""))
    if signal:
        outcome, exit_status = "signal", int(signal[0])
    elif code:
        outcome, exit_status = "exit-code" if int(code[0]) else "success", int(code[0])
    else:
        outcome, exit_status = None, 0
    if fields.get("state") == "running":
        state, sub_state = "active", "running"
    elif fields.get("state") == "spawn scheduled":  # KeepAlive waits out the ThrottleInterval
        state, sub_state = "activating", "auto-restart"
    else:
        state = "failed" if outcome in ("exit-code", "signal") else "inactive"
        sub_state = fields.get("state")
    result = {
        "unit": fields.get("path") or str(path),
        "installed": path.exists() or bool(fields),
        "enabled": path.exists() and not _disabled(),
        "running": state == "active",
        "state": state,
        "sub_state": sub_state,
        "since": _started(pid) if state == "active" and pid else None,
        "pid": pid,
        "restarts": max(int(fields.get("runs") or 0) - 1, 0),
        "result": outcome,
        "exit_status": exit_status,
        "command": _program(path),
        "log_file": str(log_path()),
        "logs": [],
        "logs_error": None,
    }
    return _with_logs(result, lines)


def tail_command(lines: int, follow: bool = False) -> list[str]:
    return ["tail", "-n", str(lines), *(["-F"] if follow else []), str(log_path())]


def file_entries(lines: Iterable[str]) -> Iterator[dict]:
    """The agent's log lines as ``{at, pid, priority, message}``, like the
    journal's. A line the client did not stamp, such as a traceback, gets the
    time of the line before it."""
    at = None
    for line in lines:
        line = line.rstrip("\n")
        stamped = STAMPED.fullmatch(line)
        if stamped:
            at = datetime.fromisoformat(stamped[1]).astimezone(UTC).isoformat()
            line = stamped[2]
        if line:
            yield {"at": at, "pid": None, "priority": 6, "message": line}


def _agent_logs(lines: int) -> list[dict]:
    if not log_path().exists():
        raise Unavailable(f"There is no {log_path()} yet.")
    return list(file_entries(_run(tail_command(lines)).splitlines()))
