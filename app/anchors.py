"""Where a review thread's span is in the live text now (docs/DESIGN.md §13, Anchoring).

pycrdt's StickyIndex cannot anchor a span (live/ydoc.py says why). A thread is
anchored to a snapshot instead: a commit, the file's path there and a span in
code points of that file's text (the base). Every request diffs the base text
against the live text of the thread's file and maps both ends with
diff-match-patch's ``diff_xIndex``.

``diff_xIndex`` moves a position inside deleted text to where the deletion
was, in front of anything inserted in its place. So a span whose words were
rewritten maps onto the new words, and a span that was deleted as a whole maps
to nothing, which makes the thread outdated. Text that the diff inserts exactly
at the span's end joins it, text inserted exactly at its start does not.

A list of threads asks for the same texts and diffs many times. An Anchorer
lives for one request and keeps the base texts per commit and path, the live
files per project and the diffs per pair, so fifty threads on one file cost
one diff per base commit. Base texts never change (commits are fixed), the
live text is read once, so every thread of a request sees the same state.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from diff_match_patch import diff_match_patch
from pycrdt import Doc

from .config import get_settings
from .live import git, rooms, ydoc
from .models import Thread

# Diffing gives up after this long and returns a coarser diff, which still maps
# every position, only less precisely around big rewrites.
DIFF_TIMEOUT_SECONDS = 1.0
_HUNK_RE = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)$")


@dataclass
class Span:
    """A thread's current span (or its base span, when outdated)."""

    file_id: str | None
    path: str
    start: int
    end: int
    start16: int
    end16: int
    line_start: int
    line_end: int
    text: str
    changed: bool
    outdated: bool


@dataclass
class LiveFiles:
    """A project's live text files as one read saw them."""

    by_id: dict[str, tuple[str, str]]  # file id -> (path, text)
    by_path: dict[str, str]  # path -> file id


# --- Lines ---------------------------------------------------------------------------


def line_of(text: str, index: int) -> int:
    """The 1-based line that the code point ``index`` of ``text`` is on."""
    return text.count("\n", 0, index) + 1


def lines_of(text: str, start: int, end: int) -> tuple[int, int]:
    """The first and last line of ``[start, end)``. A span that ends with a
    newline ends on that newline's line, not on the next one."""
    first = line_of(text, start)
    last = end - 1 if end > start and text[end - 1] == "\n" else end
    return first, max(first, line_of(text, last))


def line_count(text: str) -> int:
    """Lines as an editor numbers them. A final newline does not start another
    line, and an empty file has one empty line."""
    count = text.count("\n") + 1
    return count - 1 if count > 1 and text.endswith("\n") else count


def line_span(text: str, line_start: int, line_end: int) -> tuple[int, int]:
    """``[start, end)`` of whole lines, without the last line's newline (or
    carriage return). Raises ValueError for lines the text does not have."""
    count = line_count(text)
    if not 1 <= line_start <= line_end <= count:
        raise ValueError(f"The file has {count} lines, so lines {line_start} to {line_end} do not exist")
    lines = text.split("\n")
    start = sum(len(line) + 1 for line in lines[: line_start - 1])
    end = sum(len(line) + 1 for line in lines[:line_end]) - 1
    if end > start and text[end - 1] == "\r":
        end -= 1
    return start, end


# --- Mapping -------------------------------------------------------------------------


def _dmp() -> diff_match_patch:
    dmp = diff_match_patch()
    dmp.Diff_Timeout = DIFF_TIMEOUT_SECONDS
    return dmp


def map_index(dmp: diff_match_patch, diffs: list[tuple[int, str]], index: int) -> int:
    """``index`` in the diff's old text, as a position in its new text."""
    if not diffs:  # both texts empty, diff_xIndex would fail on that
        return 0
    return dmp.diff_xIndex(diffs, index)


def follow_path(project_id: str, old: str, new: str, path: str) -> str | None:
    """What ``path`` at commit ``old`` is called at commit ``new``, following
    git's rename detection (modified files count when half of them stayed).
    None when it was deleted. A thread made on an old version needs this to
    find its file in the live document, which only knows today's paths."""
    out = git.run(project_id, "diff", "--no-color", "--no-ext-diff", "-M", "--name-status", "-z", old, new, "--")
    fields = out.decode(errors="replace").split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        if status in "RC":
            if fields[i + 1] == path and status == "R":
                return fields[i + 2]
            i += 3
        else:
            if fields[i + 1] == path:
                return None if status == "D" else path
            i += 2
    return path


def read_live(doc: Doc) -> LiveFiles:
    """The text files of a document, as a snapshot would write them."""
    by_id: dict[str, tuple[str, str]] = {}
    by_path: dict[str, str] = {}
    for path, entry in ydoc.materialize(doc).items():
        if entry.kind == "text":
            by_id[entry.id] = (path, entry.text or "")
            by_path[path] = entry.id
    return LiveFiles(by_id, by_path)


class Anchorer:
    """Finds threads' current spans, with the caches of one request."""

    def __init__(self) -> None:
        self.dmp = _dmp()
        self._base: dict[tuple[str, str, str], str | None] = {}
        self._live: dict[str, LiveFiles] = {}
        self._diffs: dict[tuple[str, str, str, str], list[tuple[int, str]]] = {}

    def base_text(self, project_id: str, commit: str, path: str) -> str | None:
        """The text of ``path`` at ``commit``, or None when it is not there or
        not text."""
        key = (project_id, commit, path)
        if key not in self._base:
            data = git.show(project_id, commit, path) if git.safe_ref(commit) else None
            text = None
            if data is not None and ydoc.is_text(data, get_settings().max_text_bytes):
                text = data.decode()
            self._base[key] = text
        return self._base[key]

    def live(self, project_id: str) -> LiveFiles:
        if project_id not in self._live:
            try:
                with rooms.get(project_id).read() as doc:
                    self._live[project_id] = read_live(doc)
            except LookupError:  # the project was deleted meanwhile
                self._live[project_id] = LiveFiles({}, {})
        return self._live[project_id]

    def use_live(self, project_id: str, files: LiveFiles) -> None:
        """Map against ``files`` instead of reading the room, for a caller that
        holds the room's lock and is about to edit (apply)."""
        self._live[project_id] = files
        for key in [k for k in self._diffs if k[0] == project_id]:
            del self._diffs[key]

    def _diff(self, project_id: str, commit: str, path: str, file_id: str, old: str, new: str) -> list[tuple[int, str]]:
        key = (project_id, commit, path, file_id)
        if key not in self._diffs:
            self._diffs[key] = self.dmp.diff_main(old, new)
        return self._diffs[key]

    def locate(self, thread: Thread) -> Span:
        """The thread's span in the live text now. Outdated when the file is
        gone or the span mapped to nothing while the quote was not empty, and
        then it is the base span."""
        base = self.base_text(thread.project_id, thread.base_commit, thread.base_path)
        files = self.live(thread.project_id)
        file_id = thread.file_id if thread.file_id in files.by_id else None
        # A thread made on a commit whose file had moved on carries no id. A
        # file that was deleted and created again has a new one. Both are
        # found by the base path.
        if file_id is None:
            file_id = files.by_path.get(thread.base_path)
        if base is None or file_id is None:
            return self._outdated(thread, base)
        path, text = files.by_id[file_id]
        diffs = self._diff(thread.project_id, thread.base_commit, thread.base_path, file_id, base, text)
        start = map_index(self.dmp, diffs, thread.base_start)
        end = max(start, map_index(self.dmp, diffs, thread.base_end))
        if start == end and thread.quote:
            return self._outdated(thread, base)
        current = text[start:end]
        line_start, line_end = lines_of(text, start, end)
        return Span(
            file_id=file_id,
            path=path,
            start=start,
            end=end,
            start16=ydoc.utf16_index(text, start),
            end16=ydoc.utf16_index(text, end),
            line_start=line_start,
            line_end=line_end,
            text=current,
            changed=current != thread.quote,
            outdated=False,
        )

    @staticmethod
    def _outdated(thread: Thread, base: str | None) -> Span:
        def u16(index: int) -> int:
            return ydoc.utf16_index(base, index) if base is not None else index

        return Span(
            file_id=thread.file_id,
            path=thread.base_path,
            start=thread.base_start,
            end=thread.base_end,
            start16=u16(thread.base_start),
            end16=u16(thread.base_end),
            line_start=thread.base_line_start,
            line_end=thread.base_line_end,
            text="",
            changed=bool(thread.quote),
            outdated=True,
        )


# --- Word diff -----------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    """No user or system configuration, as in live/git.py, so a developer's
    diff settings cannot change the format."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


def word_diff(old: str, new: str, old_line: int = 1, new_line: int = 1) -> str:
    """git's ``--word-diff=porcelain`` between two strings, as one hunk with all
    of both in it. The hunk header counts lines from ``old_line`` and
    ``new_line``, where the strings start in their files, so the web app shows
    the file's line numbers. Empty when the strings are the same."""
    if old == new:
        return ""
    context = max(old.count("\n"), new.count("\n")) + 1
    with tempfile.TemporaryDirectory(prefix="meertex-worddiff-") as tmp:
        Path(tmp, "a").write_bytes(old.encode())
        Path(tmp, "b").write_bytes(new.encode())
        proc = subprocess.run(
            ["git", "diff", "--no-index", "--no-color", "--no-ext-diff", "--word-diff=porcelain", f"-U{context}",
             "--", "a", "b"],
            cwd=tmp,
            env=_git_env(),
            capture_output=True,
            timeout=60,
            check=False,
        )
    # --no-index exits 1 when the files differ.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git diff failed: {proc.stderr.decode(errors='replace').strip()}")
    lines = proc.stdout.decode(errors="replace").splitlines()
    out: list[str] = []
    for line in lines:
        if not out:
            # git's header (diff --git, index, ---, +++) is left out, as the
            # web app skips it anyway, and the hunk gets the file's lines.
            m = _HUNK_RE.match(line)
            if m is None:
                continue
            a, a_count, b, b_count, rest = m.groups()
            line = f"@@ -{int(a) + old_line - 1}{a_count or ''} +{int(b) + new_line - 1}{b_count or ''} @@{rest}"
        out.append(line)
    return "\n".join(out) + "\n" if out else ""
