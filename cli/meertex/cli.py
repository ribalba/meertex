"""The ``meertex`` command line (docs/DESIGN.md §12).

Commands are thin. They build a request, call the API and print the answer.
With ``--json`` they print the server's JSON unchanged, plus a few local
fields such as ``checkout_dir``, because scripts and Claude read that and the
server is the one place that defines the shapes. Without it they print
compact tables for a person.
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import date as Date
from pathlib import Path
from urllib.parse import quote

from . import (
    __version__,
    cmd_compile,
    cmd_hold,
    cmd_planning,
    cmd_refs,
    cmd_reminders,
    cmd_reviews,
    config,
    output,
    service,
)
from .api import Api, ApiError, project_path
from .config import ConfigError
from .output import paint, print_json, table
from .sync import CheckoutLock, Log, run_daemon, sync_many_once, sync_once

DATE_KINDS = (
    "abstract", "submission", "supplementary", "rebuttal_start", "rebuttal_end", "notification",
    "revision", "camera_ready", "conference_start", "conference_end", "other",
)
VERSION_KINDS = ("submission", "rebuttal", "revision", "camera_ready", "arxiv", "snapshot", "other")
OUTCOMES = ("accepted", "rejected", "revision", "withdrawn", "not_submitted", "none")
FIELDS = (
    "name", "title", "paper", "conference", "conference_name", "year", "track", "cfp_url",
    "conference_url", "submission_url", "location", "publisher", "page_limit", "template",
    "review_model", "notes", "main_file", "outcome", "labels", "archived", "doi", "publication_url",
)
WHEN = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2}(?::\d{2})?))?(?:@(\S+))?$")


class UsageError(Exception):
    """A command line that cannot work as given."""


# --- Small helpers -------------------------------------------------------------------


def api() -> Api:
    return Api(config.server(), config.token())


def err(message: str) -> None:
    print(paint(message, output.RED, sys.stderr), file=sys.stderr)


def note(message: str) -> None:
    print(message, file=sys.stderr)


def prompt(label: str) -> str:
    print(label, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        raise UsageError(f"No answer given for {label.strip(' ?')}.")
    return line.strip()


def interactive() -> bool:
    return sys.stdin.isatty()


def project_ref(value: str) -> str:
    """A project name or id as given, or the project of a checkout folder when
    the value is a path (``.``, ``./paper``, ``~/papers/x``)."""
    if value == "." or "/" in value or value.startswith("~"):
        root = config.find_checkout(Path(value).expanduser())
        if root is None:
            raise UsageError(f"{value} is not inside a meertex checkout.")
        meta = config.read_project(root) or {}
        return meta.get("project_id") or meta.get("name") or value
    return value


def parse_when(value: str) -> dict:
    """``YYYY-MM-DD[THH:MM][@tz]`` as the API takes it. No tz means AoE."""
    m = WHEN.match(value.strip())
    if not m:
        raise UsageError(f"{value} is not a date. Use YYYY-MM-DD[THH:MM][@tz], "
                         "for example 2027-01-15 or 2027-01-15T23:59@Europe/Berlin.")
    day, clock, tz = m.groups()
    try:
        Date.fromisoformat(day)
    except ValueError as exc:
        raise UsageError(f"{value} is not a date ({exc}).") from exc
    tz = tz or "AoE"
    if clock:
        hh, rest = clock.split(":", 1)
        return {"due_at": f"{day}T{int(hh):02d}:{rest}", "tz": tz}
    return {"date": day, "tz": tz}


def parse_date_spec(spec: str) -> dict:
    """``kind[:label]=YYYY-MM-DD[THH:MM][@tz]`` as an entry of ``dates``."""
    key, sep, value = spec.partition("=")
    if not sep or not value:
        raise UsageError(f"{spec} is not a date. Use kind=YYYY-MM-DD[THH:MM][@tz], for example submission=2027-01-15.")
    kind, _, label = key.partition(":")
    kind = kind.strip().lower().replace("-", "_")
    if kind not in DATE_KINDS:
        raise UsageError(f"Unknown date kind {kind!r}. Kinds are: {', '.join(DATE_KINDS)}.")
    body = {"kind": kind, **parse_when(value)}
    if label.strip():
        body["label"] = label.strip()
    elif kind == "other":
        raise UsageError("A date of kind other needs a label, for example other:Artifact=2027-03-01.")
    return body


def parse_bool(value: str) -> bool:
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off", ""):
        return False
    raise UsageError(f"{value} is not a yes or no.")


def parse_field(item: str) -> tuple[str, object]:
    key, sep, value = item.partition("=")
    key = key.strip().replace("-", "_")
    if not sep:
        raise UsageError(f"Expected field=value, got {item!r}.")
    if key not in FIELDS:
        raise UsageError(f"Unknown field {key!r}. Fields are: {', '.join(FIELDS)}.")
    if key == "year":
        try:
            return key, int(value)
        except ValueError as exc:
            raise UsageError(f"year must be a number, not {value!r}.") from exc
    if key == "archived":
        return key, parse_bool(value)
    if key == "labels":
        return key, [v.strip() for v in value.split(",") if v.strip()]
    if key == "outcome":
        value = value.strip().lower().replace("-", "_").replace(" ", "_")
        if value not in OUTCOMES and value not in ("", "null"):
            raise UsageError(f"Unknown outcome {value!r}. Outcomes are: {', '.join(OUTCOMES)}.")
        return key, None if value in ("none", "null", "") else value
    return key, value


def load_json_file(name: str) -> dict:
    try:
        text = sys.stdin.read() if name == "-" else Path(name).expanduser().read_text()
        value = json.loads(text)
    except OSError as exc:
        raise UsageError(f"Cannot read {name} ({exc.strerror or exc}).") from exc
    except ValueError as exc:
        raise UsageError(f"{name} is not valid JSON ({exc}).") from exc
    if not isinstance(value, dict):
        raise UsageError(f"{name} must hold a JSON object, the body of POST /api/projects.")
    return value


def project_body(args) -> dict:
    """The body of POST /api/projects. ``--from-json`` comes first and the
    flags go on top. A ``--date`` replaces the JSON's date of the same kind
    (and label)."""
    body = load_json_file(args.from_json) if args.from_json else {}
    for key in ("title", "conference", "year", "paper", "track", "name", "starter"):
        value = getattr(args, key, None)
        if value is not None:
            body[key] = value
    for item in args.set or []:
        key, value = parse_field(item)
        body[key] = value
    if getattr(args, "planned", False):
        body["planned"] = True
    if args.label:
        body["labels"] = list(args.label)
    if args.folder is not None:
        body["folder"] = args.folder
    if args.date:
        new = [parse_date_spec(d) for d in args.date]
        keys = {(d["kind"], d.get("label")) for d in new}
        kept = [d for d in body.get("dates") or [] if (d.get("kind"), d.get("label")) not in keys
                and not (d.get("kind") != "other" and (d.get("kind"), None) in keys)]
        body["dates"] = kept + new
    return body


def resolve_date(project: dict, key: str) -> dict:
    dates = project.get("dates") or []
    exact = [d for d in dates if d.get("id") == key]
    if exact:
        return exact[0]
    low = key.lower().replace("-", "_")
    for candidates in (
        [d for d in dates if len(key) >= 4 and str(d.get("id", "")).startswith(key)],
        [d for d in dates if d.get("kind") == low],
        [d for d in dates if (d.get("label") or "").lower() == key.lower()],
    ):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            listed = ", ".join(f"{d['id'][:8]} ({d.get('label') or d.get('kind')})" for d in candidates)
            raise UsageError(f"{key} matches several dates: {listed}. Use the id.")
    raise UsageError(f"{project.get('name')} has no date {key!r}.")


def find_folder(folders: list[dict], path: str) -> dict | None:
    want = path.strip("/").lower()
    for f in folders:
        if (f.get("path") or f.get("name") or "").strip("/").lower() == want:
            return f
    return None


def sync_log(args, quiet: bool = False) -> Log:
    return Log(as_json=args.json, stream=sys.stderr if args.json else sys.stdout, quiet=quiet or args.json)


# --- Checkouts -------------------------------------------------------------------------


def target_dir(name: str, target: str | None) -> Path:
    root = Path(target).expanduser() if target else config.papers_dir() / name
    root = root.absolute()
    if root.exists():
        if not root.is_dir():
            raise UsageError(f"{root} exists and is not a folder.")
        if any(root.iterdir()):
            raise UsageError(f"{root} is not empty. Check out into an empty or a new folder.")
    return root


def start_checkout(root: Path, project: dict, server: str, log: Log) -> dict:
    """Write ``.meertex/project.json``, sync once and register the folder."""
    root.mkdir(parents=True, exist_ok=True)
    meta = {
        "project_id": project["id"],
        "name": project["name"],
        "server": server,
        "main_file": project.get("main_file") or "main.tex",
    }
    config.write_project(root, meta)
    result = asyncio.run(sync_once(root, config.token(), log))
    config.register_checkout(root, meta)
    return result


def print_checkout(root: Path, result: dict) -> None:
    files = len((result.get("report") or {}).get("written") or [])
    if result.get("ok"):
        print(f"  checkout  {root}" + (f" ({output.count(files, 'file')})" if files else ""))
    else:
        print(f"  checkout  {root}")
        err(f"The first sync did not finish ({result.get('error')}). "
            f"Try again with meertex sync --once {root}")


# --- Commands ------------------------------------------------------------------------


def cmd_login(args) -> int:
    server = config.normalize_server(args.server) if args.server else config.server(required=False)
    if not server:
        if not interactive():
            raise UsageError("Pass --server URL the first time.")
        server = config.normalize_server(prompt("Server address (https://...)? "))
    if args.token:
        with Api(server, args.token.strip()) as a:
            me = a.get("/api/auth/me")
        token = args.token.strip()
    else:
        email = args.email or prompt("Email address? ")
        with Api(server, None) as a:
            a.post("/api/auth/login", {"email": email, "client": "cli"})
            note(f"A sign-in code is on its way to {email}.")
            code = prompt("Code from the email? ")
            a.post("/api/auth/verify-code", {"email": email, "code": code})
            me = a.post("/api/auth/api-token")
        token = me.get("api_token")
        if not token:
            raise ApiError(0, "The server did not hand out an API token.")
    path = config.save_token(token)
    config.save_server(server)
    if args.json:
        print_json({"server": server, "email": me.get("email"), "token_file": str(path)})
    else:
        print(f"Signed in as {me.get('email')} on {server}. The token is in {path}.")
    return 0


def cmd_projects(args) -> int:
    params = {}
    for key in ("outcome", "label", "folder", "q", "year", "conference"):
        value = getattr(args, key)
        if value is not None:
            params[key] = value
    if args.all:
        params["archived"] = "all"
    elif args.archived:
        params["archived"] = "true"
    with api() as a:
        projects = a.get("/api/projects", params=params)
    if args.json:
        print_json(projects)
        return 0
    if not projects:
        print("No projects.")
        return 0
    rows = []
    for p in projects:
        nd = p.get("next_date") or {}
        q = p.get("question_count") or 0
        rows.append([
            paint(p.get("name") or "", output.BOLD),
            output.venue(p),
            output.outcome(p.get("outcome")),
            nd.get("label") or nd.get("kind") or "",
            output.countdown(nd.get("seconds_left")) if nd else "",
            paint(f"{q} ?", output.AMBER) if q else "",
            output.truncate(p.get("title") or "", 48),
        ])
    print(table(rows, ["NAME", "VENUE", "OUTCOME", "NEXT", "", "", "TITLE"]))
    return 0


def print_project(p: dict) -> None:
    print(paint(p.get("name") or "", output.BOLD))
    if p.get("title"):
        print(p["title"])
    where = ", ".join(x for x in (output.venue(p), p.get("track"), p.get("location")) if x)
    if where:
        print(where)
    print()
    rows = []
    if p.get("outcome"):
        rows.append(["outcome", output.outcome(p["outcome"]) + (f" ({output.day(p.get('outcome_at'))})"
                                                                if p.get("outcome_at") else "")])
    nd = p.get("next_date")
    if nd:
        label = nd.get("label") or nd.get("kind")
        rows.append(["next", f"{label} {output.countdown(nd.get('seconds_left'))} ({output.when(nd)})"])
    for key in ("url", "folder_path", "role", "main_file", "file_count", "publisher", "page_limit", "template",
                "review_model", "cfp_url", "conference_url", "submission_url", "doi", "publication_url"):
        value = p.get(key)
        if value not in (None, "", []):
            rows.append([key.replace("_path", "").replace("_", " "), str(value)])
    if p.get("labels"):
        rows.append(["labels", ", ".join(p["labels"])])
    if p.get("forked_from"):
        rows.append(["forked from", p["forked_from"].get("name") or p["forked_from"].get("id")])
    if p.get("archived"):
        rows.append(["archived", "yes"])
    print(table(rows))

    dates = p.get("dates") or []
    if dates:
        print("\n" + paint("Dates", output.BOLD))
        rows = []
        for d in dates:
            moved = f"moved {len(d['changes'])}x" if d.get("changes") else ""
            rows.append([d.get("id", "")[:8], d.get("kind") or "", d.get("label") or "", output.when(d),
                         output.countdown(d.get("seconds_left")), d.get("answer") or "", moved])
        print(table(rows, ["ID", "KIND", "LABEL", "DUE", "", "ANSWER", ""], indent="  "))
    questions = p.get("questions") or []
    if questions:
        print("\n" + paint("Questions", output.BOLD))
        for q in questions:
            print(f"  {paint(q.get('kind') or '', output.AMBER)}  {q.get('text')}")
            print(paint(f"    meertex answer {p.get('name')} {q.get('kind')} "
                        f"{'|'.join(q.get('answers') or [])}", output.DIM))
    versions = p.get("versions") or []
    if versions:
        print("\n" + paint("Versions", output.BOLD))
        print_versions(versions, indent="  ")
    members = p.get("members") or []
    if len(members) > 1:
        print("\n" + paint("Members", output.BOLD))
        print(table([[m.get("email") or "", m.get("role") or "", m.get("name") or ""] for m in members],
                    indent="  "))
    if p.get("notes"):
        print("\n" + paint("Notes", output.BOLD))
        print("\n".join("  " + line for line in p["notes"].splitlines()))


def cmd_show(args) -> int:
    with api() as a:
        p = a.get(project_path(project_ref(args.project)))
    if args.json:
        print_json(p)
    else:
        print_project(p)
    return 0


def print_versions(versions: list[dict], indent: str = "") -> None:
    rows = []
    for v in versions:
        rows.append([str(v.get("number", "")), v.get("label") or v.get("kind") or "", v.get("tag") or "",
                     output.day(v.get("created_at")), (v.get("commit") or "")[:8],
                     "pdf" if v.get("pdf_name") else "", output.truncate(v.get("note") or "", 40)])
    print(table(rows, ["#", "VERSION", "TAG", "CREATED", "COMMIT", "", "NOTE"], indent=indent))


def _create(args, path: str, verb: str) -> int:
    body = project_body(args)
    server = config.server()
    if args.dry_run:
        body["dry_run"] = True
        with api() as a:
            res = a.post(path, body)
        if args.json:
            print_json(res)
        else:
            print(res.get("name") if isinstance(res, dict) else res)
        return 0
    kit = _template_zip(args, body)
    with api() as a:
        if args.checkout is not None:
            # Fail before creating anything. The default folder needs the name,
            # which a dry run tells.
            name = body.get("name")
            if not name and not args.checkout:
                name = a.post(path, {**body, "dry_run": True}).get("name")
            target_dir(name or "", args.checkout or None)
        project = a.post(path, body)
        imported = _import_zip(a, project, kit) if kit else None
    if imported is not None and imported.get("main_file"):
        # The checkout's ignore rules follow the main file the kit brought.
        project = {**project, "main_file": imported["main_file"]}
    result = dict(project)
    if imported is not None:
        result["imported"] = imported
    result["checkout_dir"] = None
    sync_result = None
    if args.checkout is not None:
        try:
            root = target_dir(project["name"], args.checkout or None)
        except UsageError as exc:
            raise UsageError(f"{exc} {project['name']} was created. "
                             f"Check it out with meertex checkout {project['name']} DIR") from exc
        sync_result = start_checkout(root, project, server, sync_log(args, quiet=True))
        result["checkout_dir"] = str(root)
        result["sync"] = {"ok": sync_result.get("ok"), "error": sync_result.get("error")}
    if args.json:
        print_json(result)
    else:
        print(f"{verb} {paint(project['name'], output.BOLD)}")
        if project.get("url"):
            print(f"  url       {project['url']}")
        if imported is not None:
            print(f"  template  {len(imported.get('files') or [])} files from {Path(args.zip).name}, "
                  f"main file {imported.get('main_file') or '(none found)'}")
            others = [c for c in imported.get("main_candidates") or [] if c != imported.get("main_file")]
            if others:
                print(f"  other papers in the kit: {', '.join(others)} (meertex set {project['name']} main_file=...)")
        if sync_result is not None:
            print_checkout(Path(result["checkout_dir"]), sync_result)
    return 0 if sync_result is None or sync_result.get("ok") else 1


# The server takes a request body of at most 50 MB (MAX_BLOB_BYTES).
MAX_ZIP_BYTES = 50 * 1024 * 1024


def _template_zip(args, body: dict) -> bytes | None:
    """The bytes of --zip, checked before anything is created."""
    zip_arg = getattr(args, "zip", None)
    if not zip_arg:
        return None
    if body.get("starter") == "basic":
        raise UsageError("--zip and --starter basic both give the starting files. Use one of them.")
    path = Path(zip_arg).expanduser()
    if not path.is_file():
        raise UsageError(f"{path} is not a file.")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise UsageError(f"{path} is larger than 50 MB, the most a template zip may be.")
    return path.read_bytes()


def _import_zip(a, project: dict, kit: bytes) -> dict:
    """Import the template into the project just made. When that fails the
    project goes again, so a failed import leaves nothing behind."""
    try:
        return a.post(project_path(project["id"]) + "/import", None, content=kit,
                      headers={"Content-Type": "application/zip"})
    except ApiError as exc:
        try:
            a.delete(project_path(project["id"]), params={"confirm": project["name"]})
        except ApiError:
            pass
        detail = str(exc.detail).rstrip(".")
        raise UsageError(f"The template could not be imported: {detail}. Nothing was created.") from exc


def cmd_new(args) -> int:
    return _create(args, "/api/projects", "Created")


def cmd_fork(args) -> int:
    return _create(args, project_path(project_ref(args.project)) + "/fork", "Forked into")


def cmd_checkout(args) -> int:
    server = config.server()
    with api() as a:
        project = a.get(project_path(project_ref(args.project)))
    root = target_dir(project["name"], args.dir)
    result = start_checkout(root, project, server, sync_log(args, quiet=True))
    if args.json:
        print_json({**project, "checkout_dir": str(root), "sync": result})
    else:
        print(f"Checked out {paint(project['name'], output.BOLD)}")
        print_checkout(root, result)
    return 0 if result.get("ok") else 1


def guess_main_file(root: Path) -> str | None:
    """The one top-level .tex file with a \\documentclass, when main.tex is not there."""
    if (root / "main.tex").exists():
        return None
    found = []
    for f in sorted(root.glob("*.tex")):
        try:
            if "\\documentclass" in f.read_text(errors="replace")[:20000]:
                found.append(f.name)
        except OSError:
            continue
    return found[0] if len(found) == 1 else None


def cmd_link(args) -> int:
    root = Path(args.dir).expanduser().absolute()
    if not root.is_dir():
        raise UsageError(f"{root} is not a folder.")
    meta = config.read_project(root)
    if meta:
        raise UsageError(f"{root} is a checkout of {meta.get('name')} already.")
    server = config.server()
    with api() as a:
        if args.project:
            project = a.get(project_path(project_ref(args.project)))
            count = project.get("file_count")
            if count is None:
                count = len(a.get(project_path(project["id"]) + "/files") or [])
            if count:
                raise UsageError(f"{project['name']} has {count} files already. link needs an empty project. "
                                 f"Use meertex checkout {project['name']} DIR instead.")
        else:
            body = project_body(args)
            for key, label in (("title", "Title"), ("conference", "Conference (acronym)"), ("year", "Year")):
                if not body.get(key):
                    if not interactive():
                        raise UsageError("Pass --title, --conference and --year, or --project.")
                    value = prompt(f"{label}? ")
                    body[key] = int(value) if key == "year" and value.isdigit() else value
            main = guess_main_file(root)
            if main and "main_file" not in body:
                body["main_file"] = main
            project = a.post("/api/projects", body)
    result = start_checkout(root, project, server, sync_log(args, quiet=True))
    if args.json:
        print_json({**project, "checkout_dir": str(root), "sync": result})
    else:
        print(f"Linked {root} to {paint(project['name'], output.BOLD)}")
        if project.get("url"):
            print(f"  url       {project['url']}")
        report = result.get("report") or {}
        sent = len(report.get("added") or []) + len(report.get("sent") or [])
        if result.get("ok"):
            print(f"  sent      {output.count(sent, 'file')}")
        else:
            err(f"The first sync did not finish ({result.get('error')}). "
                f"Try again with meertex sync --once {root}")
    return 0 if result.get("ok") else 1


def _checkout_roots(dirs: list[str]) -> list[Path]:
    roots = []
    for d in dirs:
        root = config.find_checkout(Path(d).expanduser())
        if root is None:
            raise UsageError(f"{d} is not inside a meertex checkout.")
        roots.append(root)
    return roots


def cmd_sync(args) -> int:
    token = config.token(required=args.once)
    if args.hold:
        args.dirs = cmd_hold.hold_for_sync(args)
    if args.dirs:
        roots = _checkout_roots(args.dirs)
    else:
        roots = None
    if not args.once:
        log = Log(as_json=args.json)
        return asyncio.run(run_daemon(roots, log))
    if roots is None:
        roots = []
        for c in config.load_checkouts():
            root = Path(c["dir"])
            if (root / config.META_DIR / config.PROJECT_FILE).is_file():
                roots.append(root)
            else:
                note(f"Skipping {root}. It is not a checkout any more.")
    if not roots:
        if args.json:
            print_json({"ok": True, "checkouts": []})
        else:
            print("No checkouts registered.")
        return 0
    results = asyncio.run(sync_many_once(roots, token, sync_log(args)))
    ok = all(r.get("ok") for r in results)
    if args.json:
        print_json({"ok": ok, "checkouts": results})
    else:
        for r in results:
            name = paint(r.get("name") or r.get("dir"), output.BOLD)
            if r.get("ok"):
                via = " (by the sync service)" if r.get("via_service") else ""
                print(f"{name}  {cmd_hold.once_state(r) or paint('in sync', output.GREEN)}{via}")
            else:
                print(f"{name}  {paint('not in sync', output.RED)}. {r.get('error')}")
    return 0 if ok else 1


def checkout_states() -> list[dict]:
    states = []
    for c in config.load_checkouts():
        root = Path(c["dir"])
        exists = (root / config.META_DIR / config.PROJECT_FILE).is_file()
        status = config.read_json(root / config.META_DIR / "status.json", {}) if exists else {}
        holder = CheckoutLock.holder(root) if exists else None
        states.append({
            "dir": str(root),
            "name": c.get("name"),
            "project_id": c.get("project_id"),
            "exists": exists,
            "running": holder is not None,
            "pid": holder,
            "mode": status.get("mode") if holder else None,
            "connected": bool(holder and status.get("connected")),
            "last_sync_at": status.get("last_sync_at"),
            "unsent": bool(status.get("unsent")),
            "pending": bool(status.get("pending")),
            "last_error": status.get("last_error"),
            **cmd_hold.hold_state(root, exists),
        })
    return states


def cmd_status(args) -> int:
    states = checkout_states()
    if args.json:
        print_json(states)
        return 0
    if not states:
        print("No checkouts on this machine. Use meertex checkout or meertex new --checkout.")
        return 0
    rows = []
    for s in states:
        if not s["exists"]:
            state = paint("missing", output.RED)
        elif not s["running"]:
            state = paint("not syncing", output.DIM)
        elif s["connected"]:
            state = paint("live", output.GREEN)
            if s["unsent"]:
                state += ", sending"
            if s["pending"]:
                state += ", blobs waiting"
        else:
            state = paint("offline", output.AMBER)
        if s["held"]:
            state += paint(", held", output.AMBER)
        rows.append([paint(s["name"] or "", output.BOLD), state, output.ago(s["last_sync_at"]), s["dir"]])
    print(table(rows, ["NAME", "STATE", "LAST SYNC", "DIR"]))
    for s in states:
        if s["last_error"] and s["exists"]:
            print(paint(f"{s['name']}  {s['last_error']}", output.DIM))
    cmd_hold.print_held(states)
    if not any(s["running"] for s in states):
        print(paint("Nothing syncs right now. Run meertex sync, or install the service with meertex service install.",
                    output.DIM))
    return 0


def cmd_set(args) -> int:
    body = dict(parse_field(item) for item in args.fields)
    if not body:
        raise UsageError("Nothing to set. Use field=value.")
    with api() as a:
        p = a.patch(project_path(project_ref(args.project)), body)
    if args.json:
        print_json(p)
    else:
        print(f"Updated {paint(p.get('name') or args.project, output.BOLD)}: {', '.join(body)}")
        if "name" in body:
            print("Checkouts keep their folder name. Rename the folder yourself if you like.")
    return 0


def _date_from(res, kind: str | None = None, date_id: str | None = None) -> dict | None:
    if isinstance(res, dict) and "kind" in res and "due_at" in res:
        return res
    if isinstance(res, dict):
        for d in res.get("dates") or []:
            if (date_id and d.get("id") == date_id) or (not date_id and d.get("kind") == kind):
                return d
    return None


def cmd_date(args) -> int:
    ref = project_ref(args.project)
    base = project_path(ref) + "/dates"
    with api() as a:
        if args.action == "add":
            body = parse_date_spec(args.spec)
            if args.label:
                body["label"] = args.label
            res = a.post(base, body)
            d = _date_from(res, body["kind"])
            verb = "Added"
        else:
            project = a.get(project_path(ref))
            d0 = resolve_date(project, args.date)
            if args.action == "rm":
                res = a.delete(f"{base}/{quote(d0['id'], safe='')}")
                if args.json:
                    print_json(res if res is not None else {"deleted": d0["id"]})
                else:
                    print(f"Removed {d0.get('label') or d0.get('kind')}")
                return 0
            body = parse_when(args.when) if args.when else {}
            if args.label:
                body["label"] = args.label
            if args.tz:
                body["tz"] = args.tz
            if not body:
                raise UsageError("Nothing to change. Give a new date, --label or --tz.")
            res = a.patch(f"{base}/{quote(d0['id'], safe='')}", body)
            d = _date_from(res, d0.get("kind"), d0["id"])
            verb = "Moved" if args.when else "Updated"
    if args.json:
        print_json(res)
    elif d:
        print(f"{verb} {d.get('label') or d.get('kind')}, due {output.when(d)} "
              f"({output.countdown(d.get('seconds_left'))})")
    else:
        print(f"{verb}.")
    return 0


def cmd_questions(args) -> int:
    with api() as a:
        questions = a.get("/api/questions")
    if args.json:
        print_json(questions)
        return 0
    if not questions:
        print("No questions waiting.")
        return 0
    for q in questions:
        print(f"{paint(q.get('project_name') or '', output.BOLD)}  {paint(q.get('kind') or '', output.AMBER)}  "
              f"{paint(output.day(q.get('due_at')), output.DIM)}")
        print(f"  {q.get('text')}")
        print(paint(f"  meertex answer {q.get('project_name')} {q.get('kind')} {'|'.join(q.get('answers') or [])}",
                    output.DIM))
    return 0


def cmd_answer(args) -> int:
    ref = project_ref(args.project)
    with api() as a:
        project = a.get(project_path(ref))
        d = resolve_date(project, args.date_ref)
        body: dict = {"answer": args.answer}
        if args.date:
            body |= parse_when(args.date)
        version = {k: v for k, v in (("at", args.at), ("note", args.note)) if v}
        if version:
            body["version"] = version
        res = a.post(f"{project_path(ref)}/dates/{quote(d['id'], safe='')}/answer", body)
    if args.json:
        print_json(res)
        return 0
    name = res.get("name") or project.get("name")
    print(f"Answered {d.get('label') or d.get('kind')} with {args.answer}")
    before = {v.get("id") for v in project.get("versions") or []}
    for v in res.get("versions") or []:
        if v.get("id") not in before:
            print(f"  version  {v.get('label')} (tag {v.get('tag')}, {(v.get('commit') or '')[:8]})")
    if res.get("outcome") != project.get("outcome"):
        print(f"  outcome  {output.outcome(res.get('outcome')) or 'open'}")
    nd = res.get("next_date")
    if nd:
        print(f"  next     {nd.get('label') or nd.get('kind')} {output.countdown(nd.get('seconds_left'))}")
    if d.get("kind") == "notification" and args.answer == "revision":
        print(paint(f"Add the revision deadline with meertex date {name} add revision=YYYY-MM-DD", output.DIM))
    if d.get("kind") == "revision" and args.answer == "yes":
        print(paint(f"Add the new notification date with meertex date {name} add notification=YYYY-MM-DD", output.DIM))
    return 0


def cmd_versions(args) -> int:
    with api() as a:
        versions = a.get(project_path(project_ref(args.project)) + "/versions")
    if args.json:
        print_json(versions)
    elif not versions:
        print("No versions yet.")
    else:
        print_versions(versions)
    return 0


def cmd_version(args) -> int:
    ref = project_ref(args.project)
    pdf = Path(args.pdf).expanduser() if args.pdf else None
    if pdf is not None and not pdf.is_file():
        raise UsageError(f"{pdf} is not a file.")
    body = {"kind": args.kind}
    for key in ("label", "note", "at", "date_id"):
        value = getattr(args, key)
        if value:
            body[key] = value
    with api() as a:
        version = a.post(project_path(ref) + "/versions", body)
        if pdf is not None:
            res = a.upload_pdf(ref, version["id"], pdf)
            if isinstance(res, dict) and res.get("id") == version["id"]:
                version = res
            else:
                version["pdf_name"] = pdf.name
    if args.json:
        print_json(version)
    else:
        print(f"Created version {version.get('number')}, {version.get('label')} "
              f"(tag {version.get('tag')}, {(version.get('commit') or '')[:8]})")
        if pdf is not None:
            print(f"  pdf  {pdf.name}")
    return 0


def _word_lines(text: str) -> list[str]:
    """git's ``--word-diff=porcelain`` as people read it: each line of the text
    once, removed words as [-...-] and added ones as {+...+} (in colour on a
    terminal). The porcelain format puts every piece on a line of its own and
    marks the real line ends with ``~``."""
    color = output.use_color()
    lines: list[str] = []
    current: list[str] = []
    in_hunk = False
    for line in text.splitlines():
        # Everything before the first hunk is git's header (index, modes, renames).
        if not in_hunk and not line.startswith("@@"):
            continue
        in_hunk = True
        if line.startswith("@@"):
            if current:
                lines.append("".join(current))
                current = []
            lines.append(paint(line, output.CYAN) if color else line)
        elif line == "~":
            lines.append("".join(current))
            current = []
        elif line.startswith("-"):
            current.append(paint(line[1:], output.RED) if color else f"[-{line[1:]}-]")
        elif line.startswith("+"):
            current.append(paint(line[1:], output.GREEN) if color else f"{{+{line[1:]}+}}")
        else:
            current.append(line[1:])
    if current:
        lines.append("".join(current))
    return lines


def _paint_diff(text: str, word: bool) -> str:
    if word:
        return "\n".join(_word_lines(text))
    if not output.use_color():
        return text
    lines = []
    for line in text.splitlines():
        if line.startswith(("+++", "---")):
            lines.append(paint(line, output.BOLD))
        elif line.startswith("+"):
            lines.append(paint(line, output.GREEN))
        elif line.startswith("-"):
            lines.append(paint(line, output.RED))
        elif line.startswith("@@"):
            lines.append(paint(line, output.CYAN))
        elif word and line == "~":
            lines.append(paint(line, output.DIM))
        else:
            lines.append(line)
    return "\n".join(lines)


def cmd_diff(args) -> int:
    params = {"from": args.from_ref, "to": args.to_ref, "word": "true" if args.word else "false"}
    with api() as a:
        res = a.get(project_path(project_ref(args.project)) + "/diff", params=params)
    if args.json:
        print_json(res)
        return 0
    files = (res or {}).get("files") or []
    if not files:
        print(f"No differences between {args.from_ref} and {args.to_ref}.")
        return 0
    for f in files:
        head = f.get("path") or ""
        if f.get("old_path") and f.get("old_path") != head:
            head = f"{f['old_path']} → {head}"
        print(paint(f"{head} ({f.get('status')})", output.BOLD))
        if f.get("binary"):
            print("  binary file")
        elif f.get("diff"):
            print(_paint_diff(f["diff"].rstrip("\n"), args.word))
        print()
    return 0


def cmd_folders(args) -> int:
    with api() as a:
        folders = a.get("/api/folders")
    if args.json:
        print_json(folders)
    elif not folders:
        print("No folders.")
    else:
        for f in folders:
            depth = (f.get("path") or f.get("name") or "").count("/")
            print("  " * depth + (f.get("name") or ""))
    return 0


def cmd_folder(args) -> int:
    with api() as a:
        if args.action == "add":
            path = args.path.strip("/")
            parent, _, name = path.rpartition("/")
            body = {"name": name}
            if parent:
                body["parent"] = parent
            res = a.post("/api/folders", body)
            message = f"Added {res.get('path') or path}" if isinstance(res, dict) else f"Added {path}"
        else:
            folders = a.get("/api/folders")
            src = find_folder(folders, args.path)
            if src is None:
                raise UsageError(f"No folder {args.path}.")
            if args.action == "rm":
                res = a.delete(f"/api/folders/{quote(src['id'], safe='')}")
                message = f"Removed {src.get('path')}"
            else:
                dest = args.dest.strip()
                if dest in ("", "/"):
                    body = {"parent_id": None}
                elif (target := find_folder(folders, dest)) is not None:
                    body = {"parent_id": target["id"]}
                else:
                    parent, _, name = dest.strip("/").rpartition("/")
                    parent_folder = find_folder(folders, parent) if parent else None
                    if parent and parent_folder is None:
                        raise UsageError(f"No folder {parent}.")
                    body = {"name": name, "parent_id": parent_folder["id"] if parent_folder else None}
                res = a.patch(f"/api/folders/{quote(src['id'], safe='')}", body)
                message = f"Moved {src.get('path')} to {res.get('path') if isinstance(res, dict) else dest}"
    if args.json:
        print_json(res if res is not None else {"ok": True})
    else:
        print(message)
    return 0


def cmd_mv(args) -> int:
    with api() as a:
        dest = args.folder.strip()
        if dest in ("", "/"):
            folder_id = None
        else:
            folder = find_folder(a.get("/api/folders"), dest)
            if folder is None:
                raise UsageError(f"No folder {dest}. Create it with meertex folder add {dest}")
            folder_id = folder["id"]
        res = a.put(project_path(project_ref(args.project)) + "/placement", json={"folder_id": folder_id})
    if args.json:
        print_json(res if res is not None else {"ok": True})
    else:
        print(f"Moved {args.project} to {dest.strip('/') or 'the top level'}")
    return 0


# The sync client stamps its own lines with the time. The journal has the
# time of every line already, with the day. (On macOS the service reads the
# day from the client's own stamp, so the stamp is gone from the message.)
LOG_STAMP = re.compile(r"^\d\d:\d\d:\d\d ")


def _service_state(s: dict) -> str:
    if not s["installed"]:
        return paint("not installed", output.DIM)
    if s["running"]:
        return paint("running", output.GREEN)
    if s["sub_state"] == "auto-restart":
        return paint("stopped, restarting soon", output.AMBER)
    if s["state"] == "activating":
        return paint("starting", output.AMBER)
    if s["state"] == "failed":
        return paint("failed", output.RED)
    return paint("stopped", output.DIM)


def _log_line(e: dict) -> str:
    # A line above the first stamp of an agent's log file has no time.
    at = output.parse_time(e["at"]).astimezone().strftime("%b %d %H:%M:%S") if e["at"] else " " * 15
    message = LOG_STAMP.sub("", output.ANSI.sub("", e["message"]), count=1)
    code = output.RED if e["priority"] <= 3 else output.AMBER if e["priority"] == 4 else None
    return f"{paint(at, output.DIM)}  {paint(message, code)}"


def service_status(args) -> int:
    if args.lines < 0:
        raise UsageError("--lines must be 0 or more.")
    if args.json and args.follow:
        for e in service.follow(args.lines):
            print(json.dumps(e, ensure_ascii=False), flush=True)
        return 0
    s = service.status(0 if args.follow else args.lines)
    if args.json:
        print_json(s)
        return 0 if s["running"] else 1

    head = f"{paint(service.name(), output.BOLD)}  {_service_state(s)}"
    if s["since"] and s["installed"]:
        since = output.parse_time(s["since"]).astimezone().strftime("%Y-%m-%d %H:%M")
        head += f" since {since} ({output.ago(s['since'])})"
    print(head)
    rows = []
    if s["installed"]:
        rows.append(["unit", s["unit"] + (", enabled at login" if s["enabled"] else ", not enabled at login")])
        if s["command"]:
            rows.append(["runs", s["command"]])
    if s["log_file"]:
        rows.append(["log", s["log_file"]])
    if s["pid"]:
        rows.append(["pid", str(s["pid"])])
    if s["restarts"]:
        rows.append(["restarts", str(s["restarts"])])
    if s["result"] and s["result"] != "success":
        rows.append(["last exit", f"{s['result']}, status {s['exit_status']}"])
    if rows:
        print(table(rows, indent="  "))
    if not s["installed"]:
        print(paint("Install it with meertex service install.", output.DIM))
    elif not s["running"]:
        print(paint(f"Start it with {service.start_command()}.", output.DIM))

    if args.follow:
        print()
        for e in service.follow(args.lines):
            print(_log_line(e), flush=True)
        return 0
    if args.lines:
        print()
        if s["logs_error"]:
            print(paint(f"No log. {s['logs_error']}", output.AMBER))
        elif not s["logs"]:
            print(paint("No log lines.", output.DIM))
        for e in s["logs"]:
            print(_log_line(e))
    return 0 if s["running"] else 1


def cmd_service(args) -> int:
    if args.action == "status":
        try:
            return service_status(args)
        except service.Unavailable as exc:
            raise UsageError(str(exc)) from exc
    try:
        if args.action == "install":
            res = service.install(start=not args.no_start)
        else:
            res = service.uninstall(stop=not args.no_start)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UsageError(f"{'launchctl' if service.MAC else 'systemctl'} failed ({exc}).") from exc
    if args.json:
        print_json(res)
    elif args.action == "install":
        print(f"Wrote {res['unit']}")
        print(f"Started {service.name()}" if res["started"] else
              f"Not started. Start it with {service.load_command()}")
    else:
        print(f"Removed {res['unit']}" if res["removed"] else f"There was no {res['unit']}")
    return 0


# --- Parser ----------------------------------------------------------------------------


def _project_flags(p: argparse.ArgumentParser, *, creating: bool = True, starter: bool = False) -> None:
    p.add_argument("--title")
    p.add_argument("--conference", help="acronym as the venue writes it (ICSE, EuroSys)")
    p.add_argument("--year", type=int, help="the year of the conference")
    p.add_argument("--paper", help="short name of the paper, the {paper} of the naming pattern")
    p.add_argument("--track")
    p.add_argument("--name", help="overrides the name the naming pattern would give")
    p.add_argument("--date", action="append", metavar="KIND=YYYY-MM-DD[THH:MM][@TZ]",
                   help="a date, repeatable. No time zone means AoE. Use kind:label=... for kind other")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="any project field, repeatable")
    p.add_argument("--label", action="append", help="a label, repeatable")
    p.add_argument("--folder", help="your folder for it, by path (2027/Submitted)")
    p.add_argument("--from-json", metavar="FILE", help="the body of POST /api/projects, - for stdin")
    if starter:
        p.add_argument("--starter", choices=("empty", "basic"),
                       help="the starting files, empty (default) or basic for a main.tex and a refs.bib")
        p.add_argument("--zip", metavar="FILE",
                       help="a publisher's template zip (an author kit). Its files become the paper's files")
        p.add_argument("--planned", action="store_true",
                       help="a plan: the venue is known, the paper not yet (no title needed). meertex start turns it into a paper")
    if creating:
        p.add_argument("--checkout", nargs="?", const="", metavar="DIR",
                       help="check it out, into DIR or $MEERTEX_PAPERS/<name>")
        p.add_argument("--dry-run", action="store_true", help="only print the name it would get")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    parser = argparse.ArgumentParser(
        prog="meertex", parents=[common],
        description="meertex keeps LaTeX papers with their deadlines, versions and a live synced folder.",
        epilog="Projects are addressed by name or id, or by a checkout folder such as '.'.",
    )
    parser.set_defaults(json=False, func=None)
    parser.add_argument("--version", action="version", version=f"meertex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def cmd(name: str, func, help: str, **kwargs) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, description=help, parents=[common], **kwargs)
        p.set_defaults(func=func)
        return p

    p = cmd("login", cmd_login, "Sign in and store an API token")
    p.add_argument("--server", help="the meertex address, stored for later")
    p.add_argument("--email")
    p.add_argument("--token", help="store this API token instead of signing in by email")

    p = cmd("projects", cmd_projects, "The project list with countdowns")
    p.add_argument("--outcome", help="comma separated, none for open ones")
    p.add_argument("--label")
    p.add_argument("--folder", help="folder path or id, with its subfolders")
    p.add_argument("--q", help="search name, title, conference and notes")
    p.add_argument("--year", type=int)
    p.add_argument("--conference")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--archived", action="store_true", help="only archived projects")
    group.add_argument("--all", action="store_true", help="archived and active projects")

    p = cmd("show", cmd_show, "One project with its dates, questions and versions")
    p.add_argument("project")

    p = cmd("new", cmd_new, "Create a project")
    _project_flags(p, starter=True)

    p = cmd("fork", cmd_fork, "Resubmit a project to another venue as a new project")
    p.add_argument("project")
    _project_flags(p)

    p = cmd("link", cmd_link, "Turn an existing folder into a checkout")
    p.add_argument("dir")
    p.add_argument("--project", help="an empty project to link to. Without it a new project is created")
    _project_flags(p, creating=False)

    p = cmd("checkout", cmd_checkout, "Write a project into a new folder and register it")
    p.add_argument("project")
    p.add_argument("dir", nargs="?", help="default $MEERTEX_PAPERS/<name>")

    p = cmd("sync", cmd_sync, "Keep checkouts in step with the server")
    p.add_argument("dirs", nargs="*", metavar="DIR", help="these checkouts only, default all registered")
    p.add_argument("--once", action="store_true", help="sync once, wait until nothing is pending, exit")
    p.add_argument("--hold", action="store_true",
                   help="hold the given or current checkouts first (see meertex hold), then sync")

    cmd("status", cmd_status, "Every checkout on this machine with its sync state")

    p = cmd("set", cmd_set, "Edit project fields")
    p.add_argument("project")
    p.add_argument("fields", nargs="+", metavar="FIELD=VALUE")

    p = cmd("date", cmd_date, "Add, move or remove a date")
    p.add_argument("project")
    dsub = p.add_subparsers(dest="action", metavar="add|set|rm", required=True)
    d = dsub.add_parser("add", parents=[common], help="add a date as kind=YYYY-MM-DD[THH:MM][@tz]")
    d.add_argument("spec", metavar="KIND=WHEN")
    d.add_argument("--label")
    d = dsub.add_parser("set", parents=[common], help="move a date or change its label")
    d.add_argument("date", metavar="DATE", help="id, kind or label")
    d.add_argument("when", nargs="?", metavar="WHEN", help="YYYY-MM-DD[THH:MM][@tz]")
    d.add_argument("--label")
    d.add_argument("--tz")
    d = dsub.add_parser("rm", parents=[common], help="remove a date")
    d.add_argument("date", metavar="DATE", help="id, kind or label")

    cmd("questions", cmd_questions, "The questions waiting for an answer")

    p = cmd("answer", cmd_answer, "Answer a question about a date")
    p.add_argument("project")
    p.add_argument("date_ref", metavar="DATE", help="the date's id or kind")
    p.add_argument("answer", help="yes, no, moved, accepted, rejected, revision")
    p.add_argument("--date", metavar="WHEN", help="the new date for moved, as YYYY-MM-DD[THH:MM][@tz]")
    p.add_argument("--at", help="which state the version freezes: deadline (default), now, a time or a commit")
    p.add_argument("--note", help="a note on the version this answer creates")

    p = cmd("versions", cmd_versions, "List a project's versions")
    p.add_argument("project")

    p = cmd("version", cmd_version, "Make a version (and attach the PDF that was handed in)")
    p.add_argument("project")
    p.add_argument("kind", choices=VERSION_KINDS)
    p.add_argument("--label")
    p.add_argument("--note")
    p.add_argument("--pdf", metavar="FILE")
    p.add_argument("--at", help="now (default), deadline, an ISO time or a commit")
    p.add_argument("--date-id", help="the date this version belongs to")

    p = cmd("diff", cmd_diff, "Diff between two refs (tag, HEAD, live or a commit)")
    p.add_argument("project")
    p.add_argument("from_ref", metavar="FROM")
    p.add_argument("to_ref", metavar="TO", nargs="?", default="live")
    p.add_argument("--word", action="store_true", help="word diff, better for LaTeX paragraphs")

    cmd("folders", cmd_folders, "Your folder tree")

    p = cmd("folder", cmd_folder, "Add, move or remove a folder")
    fsub = p.add_subparsers(dest="action", metavar="add|mv|rm", required=True)
    f = fsub.add_parser("add", parents=[common], help="add a folder by path (2027/Submitted)")
    f.add_argument("path")
    f = fsub.add_parser("mv", parents=[common], help="move into a folder (or / for the top), or rename")
    f.add_argument("path")
    f.add_argument("dest")
    f = fsub.add_parser("rm", parents=[common], help="remove an empty folder")
    f.add_argument("path")

    p = cmd("mv", cmd_mv, "Put a project into one of your folders (/ for the top level)")
    p.add_argument("project")
    p.add_argument("folder")

    p = cmd("service", cmd_service, "Install, remove or check the background service that runs meertex sync "
                                    "(a systemd user unit, a launchd agent on macOS)")
    ssub = p.add_subparsers(dest="action", metavar="install|uninstall|status", required=True)
    for action, help in (("install", "write the service and start it"),
                         ("uninstall", "stop the service and remove it")):
        s = ssub.add_parser(action, parents=[common], help=help)
        s.add_argument("--no-start", action="store_true",
                       help="only write or remove the file, no systemctl or launchctl")
    s = ssub.add_parser("status", parents=[common], help="whether it runs, and its last log lines. Exits 1 when it does not run")
    s.add_argument("-n", "--lines", type=int, default=20, help="how many log lines, default 20")
    s.add_argument("-f", "--follow", action="store_true",
                   help="keep printing new log lines until Ctrl-C (with --json, one object per line)")

    cmd_reviews.add_commands(cmd, common)
    cmd_hold.add_commands(cmd, common)
    cmd_compile.add_commands(cmd, common)
    cmd_planning.add_commands(cmd, common)
    cmd_refs.add_commands(cmd, common)
    cmd_reminders.add_commands(cmd, common)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        raise SystemExit(2)
    as_json = getattr(args, "json", False)
    try:
        code = args.func(args) or 0
    except ApiError as exc:
        if as_json:
            print_json({"error": exc.message(), "status": exc.status, "detail": exc.detail})
        else:
            err(exc.message())
        code = 1
    except (ConfigError, UsageError) as exc:
        if as_json:
            print_json({"error": str(exc)})
        else:
            err(str(exc))
        code = 2
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)

