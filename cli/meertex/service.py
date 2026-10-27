"""The systemd user unit that runs ``meertex sync`` in the background.

The unit points at the ``meertex`` executable that installed it, so a pipx or
venv install keeps working without being on systemd's PATH. The variables
that move the CLI's configuration (MEERTEX_HOME, MEERTEX_PAPERS,
MEERTEX_SERVER) are copied into the unit, so the service syncs the same
checkouts as the shell that installed it. The token never goes into the unit.

``status`` reads the unit back from systemd and its log from the journal.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

UNIT_NAME = "meertex.service"
CARRIED_VARIABLES = ("MEERTEX_HOME", "MEERTEX_PAPERS", "MEERTEX_SERVER")
# What status asks systemctl for. Times come as UTC, so they parse the same
# in every time zone.
PROPERTIES = ("LoadState", "FragmentPath", "UnitFileState", "ActiveState", "SubState", "MainPID",
              "NRestarts", "Result", "ExecMainStatus", "ActiveEnterTimestamp", "InactiveEnterTimestamp")


class Unavailable(Exception):
    """systemctl or journalctl is missing, or the user's systemd does not answer."""


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
    for name in CARRIED_VARIABLES:
        value = os.environ.get(name)
        if value:
            if name != "MEERTEX_SERVER":
                value = str(Path(value).expanduser().resolve())
            lines.append("Environment=" + _quote(f"{name}={value}"))
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def install(start: bool = True) -> dict:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text())
    if start:
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", UNIT_NAME)
    return {"unit": str(path), "started": start}


def uninstall(stop: bool = True) -> dict:
    path = unit_path()
    if stop:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    if stop:
        _systemctl("daemon-reload")
    return {"unit": str(path), "removed": existed, "stopped": stop}


def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise Unavailable(f"{argv[0]} could not run ({exc}). The service needs systemd.") from exc
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
    """What systemd knows about the unit, with the last ``lines`` lines of its
    log. A log that cannot be read leaves ``logs`` empty and says why in
    ``logs_error``, so the unit's state still shows."""
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
        "logs": [],
        "logs_error": None,
    }
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
    return [e for e in map(log_entry, _run(journal_command(lines)).splitlines()) if e]


def follow(lines: int) -> Iterator[dict]:
    """The last ``lines`` log lines, then every new one as it is written."""
    try:
        proc = subprocess.Popen(journal_command(lines, follow=True), stdout=subprocess.PIPE, text=True)
    except OSError as exc:
        raise Unavailable(f"journalctl could not run ({exc}). The service needs systemd.") from exc
    try:
        for line in proc.stdout:
            entry = log_entry(line)
            if entry:
                yield entry
    finally:
        proc.terminate()
        proc.wait()
