"""The systemd user unit that runs ``meertex sync`` in the background.

The unit points at the ``meertex`` executable that installed it, so a pipx or
venv install keeps working without being on systemd's PATH. The variables
that move the CLI's configuration (MEERTEX_HOME, MEERTEX_PAPERS,
MEERTEX_SERVER) are copied into the unit, so the service syncs the same
checkouts as the shell that installed it. The token never goes into the unit.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "meertex.service"
CARRIED_VARIABLES = ("MEERTEX_HOME", "MEERTEX_PAPERS", "MEERTEX_SERVER")


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
