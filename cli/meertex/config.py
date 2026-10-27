"""Where the CLI keeps its server, its token and the list of checkouts.

Everything lives under one directory, ``MEERTEX_HOME`` (default ~/.meertex), so
a test or a second account can move the whole tool with one variable. A
checkout carries its own ``.meertex/project.json``, which makes a folder
self-describing even when it is not registered on this machine.
"""

import json
import os
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

META_DIR = ".meertex"
PROJECT_FILE = "project.json"
# A held checkout keeps its local changes local (docs/DESIGN.md §10, Holding).
HOLD_FILE = "hold"


class ConfigError(Exception):
    """Something the user has to fix in their setup, with a message saying how."""


def home() -> Path:
    return Path(os.environ.get("MEERTEX_HOME") or "~/.meertex").expanduser()


def papers_dir() -> Path:
    return Path(os.environ.get("MEERTEX_PAPERS") or "~/papers").expanduser()


def write_atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write ``data`` next to ``path`` and rename it into place, so a reader
    never sees half a file and a crash never leaves one behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path: Path, value: Any, mode: int | None = None) -> None:
    write_atomic(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode(), mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read {path} ({exc}).") from exc


# --- Server and token ---------------------------------------------------------


def normalize_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ConfigError("The server address is empty.")
    if "://" not in url:
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"The server address must start with http:// or https://, not {url}")
    return url


def read_config() -> dict:
    path = home() / "config.toml"
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read {path} ({exc}).") from exc


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def write_config(values: dict) -> None:
    lines = [f"{key} = {_toml_value(value)}" for key, value in values.items()]
    write_atomic(home() / "config.toml", ("\n".join(lines) + "\n").encode())


def server(required: bool = True) -> str | None:
    url = os.environ.get("MEERTEX_SERVER") or read_config().get("server")
    if url:
        return normalize_server(url)
    if required:
        raise ConfigError("No server configured. Sign in with meertex login --server https://your-meertex")
    return None


def save_server(url: str) -> None:
    values = read_config()
    values["server"] = normalize_server(url)
    write_config(values)


def token(required: bool = True) -> str | None:
    value = os.environ.get("MEERTEX_TOKEN")
    if not value:
        try:
            value = (home() / "token").read_text().strip()
        except FileNotFoundError:
            value = None
    if not value and required:
        raise ConfigError("Not signed in. Sign in with meertex login.")
    return value or None


def save_token(value: str) -> Path:
    path = home() / "token"
    write_atomic(path, (value.strip() + "\n").encode(), mode=0o600)
    return path


# --- Checkouts ------------------------------------------------------------------


def meta_dir(root: Path) -> Path:
    return root / META_DIR


def read_project(root: Path) -> dict | None:
    meta = read_json(meta_dir(root) / PROJECT_FILE)
    if meta is not None and not isinstance(meta, dict):
        raise ConfigError(f"{meta_dir(root) / PROJECT_FILE} is not a JSON object.")
    return meta


def write_project(root: Path, meta: dict) -> None:
    write_json(meta_dir(root) / PROJECT_FILE, meta)


def find_checkout(start: Path) -> Path | None:
    """The checkout that contains ``start``, looking upwards."""
    start = start.resolve()
    for folder in (start, *start.parents):
        if (meta_dir(folder) / PROJECT_FILE).is_file():
            return folder
    return None


def load_checkouts() -> list[dict]:
    """``[{dir, project_id, name, server}]`` of every checkout on this machine."""
    value = read_json(home() / "checkouts.json", [])
    if not isinstance(value, list):
        raise ConfigError(f"{home() / 'checkouts.json'} is not a JSON list.")
    return [c for c in value if isinstance(c, dict) and c.get("dir")]


def register_checkout(root: Path, meta: dict) -> None:
    root = root.resolve()
    entries = [c for c in load_checkouts() if Path(c["dir"]) != root]
    entries.append({
        "dir": str(root),
        "project_id": meta.get("project_id"),
        "name": meta.get("name"),
        "server": meta.get("server"),
    })
    entries.sort(key=lambda c: c["dir"])
    write_json(home() / "checkouts.json", entries)


def unregister_checkout(root: Path) -> bool:
    root = root.resolve()
    entries = load_checkouts()
    kept = [c for c in entries if Path(c["dir"]) != root]
    if len(kept) != len(entries):
        write_json(home() / "checkouts.json", kept)
    return len(kept) != len(entries)


# --- Holding ---------------------------------------------------------------------
#
# The marker is a file, so the CLI and a running sync service agree on it
# without talking to each other. It holds the time it was set.


def held_since(root: Path) -> str | None:
    """When ``root`` was held (ISO 8601 UTC), "" for a marker without a time,
    None when it is not held."""
    try:
        return (meta_dir(root) / HOLD_FILE).read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return ""


def set_hold(root: Path) -> tuple[str, bool]:
    """Hold ``root``. Returns the time it is held since, and whether it was
    held just now. An existing hold keeps its time."""
    since = held_since(root)
    if since is not None:
        return since, False
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_atomic(meta_dir(root) / HOLD_FILE, (since + "\n").encode())
    return since, True


def clear_hold(root: Path) -> bool:
    """Release ``root``. Returns whether it was held."""
    try:
        (meta_dir(root) / HOLD_FILE).unlink()
    except FileNotFoundError:
        return False
    return True
