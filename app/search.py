"""The search over a project's files (docs/DESIGN.md §11, Search).

A query is a regular expression in Python's syntax, or plain text. It runs
over every text file of one or more refs and reports the matching lines, like
``grep -n``. A file that is the same in several refs is searched once and
names all of them, so searching every version does not repeat a line ten
times.

The patterns come from users, so the matching runs with the ``regex``
module: it lets go of the GIL and stops after a time budget, where ``re``
would hold the one worker forever on ``(a|aa)+$``.
"""

import time
from bisect import bisect_right
from pathlib import PurePosixPath

import regex

from .live.ydoc import utf16_index

MAX_QUERY = 1000
# Matching lines sent back over all files. Further lines are only counted.
MAX_LINES = 500
# Matching lines counted per file before the file stops being searched.
MAX_FILE_LINES = 5000
# A longer line is cut to this many characters around its first match, which
# is what LaTeX paragraphs written as one line need.
SNIPPET = 300
BEFORE = 80
# Seconds of matching per search. What was found by then is sent back.
TIME_BUDGET = 5.0


class BadPattern(ValueError):
    """A query that is not a regular expression. ``args[0]`` says why."""


def compile_pattern(query: str, *, is_regex: bool = True, case: bool = False) -> regex.Pattern:
    """The query as a pattern. ``^`` and ``$`` match at every line."""
    flags = regex.MULTILINE | regex.V0
    if not case:
        flags |= regex.IGNORECASE
    try:
        return regex.compile(query if is_regex else regex.escape(query), flags)
    except regex.error as e:
        raise BadPattern(f"Not a valid regular expression: {e}") from None


def extension(path: str) -> str:
    """``tex`` for ``sections/intro.tex``, empty for a file without one."""
    return PurePosixPath(path).suffix[1:].lower()


def _snippet(line: str, spans: list[tuple[int, int]]) -> dict:
    """The line, or a window of a long line around its first match, with the
    matches as UTF-16 ranges into the text sent, which is what JavaScript
    slices."""
    a, b = 0, len(line)
    if b > SNIPPET:
        a = max(0, spans[0][0] - BEFORE)
        b = min(len(line), a + SNIPPET)
        a = max(0, b - SNIPPET)
    text = line[a:b]
    ranges = []
    for start, end in spans:
        start, end = max(start, a), min(end, b)
        if end > start:
            ranges.append([utf16_index(text, start - a), utf16_index(text, end - a)])
    return {"text": text, "ranges": ranges, "cut_before": a > 0, "cut_after": b < len(line)}


def _match_lines(
    pattern: regex.Pattern, text: str, deadline: float
) -> tuple[list[tuple[int, str, list[tuple[int, int]]]], bool]:
    """``([(line index, line, [(start, end)])], capped)`` for every line a
    match starts on. A match that runs over the end of its line is marked up
    to there. Raises TimeoutError past ``deadline``."""
    lines = text.split("\n")
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    by_line: dict[int, list[tuple[int, int]]] = {}
    capped = False
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise TimeoutError
    for m in pattern.finditer(text, timeout=timeout, concurrent=True):
        start, end = m.span()
        i = bisect_right(starts, start) - 1
        if i not in by_line and len(by_line) >= MAX_FILE_LINES:
            capped = True
            break
        line_end = starts[i] + len(lines[i])
        by_line.setdefault(i, []).append((start - starts[i], min(end, line_end) - starts[i]))
        if time.monotonic() > deadline:
            raise TimeoutError
    return [(i, lines[i].removesuffix("\r"), by_line[i]) for i in sorted(by_line)], capped


def search(sources: dict[str, dict[str, str]], pattern: regex.Pattern) -> dict:
    """Search ``{ref: {path: text}}``. Returns ``{files, lines, files_searched,
    truncated, timed_out}``. ``files`` is sorted by path, each
    ``{path, refs, count, lines: [{line, text, ranges, cut_before, cut_after}]}``,
    and ``count`` can be more than the lines sent."""
    order = {ref: n for n, ref in enumerate(sources)}
    groups: dict[tuple[str, str], list[str]] = {}
    for ref, files in sources.items():
        for path, text in files.items():
            groups.setdefault((path, text), []).append(ref)
    deadline = time.monotonic() + TIME_BUDGET
    files_out: list[dict] = []
    left = MAX_LINES
    total = 0
    searched = 0
    truncated = timed_out = False
    for (path, text), refs in sorted(groups.items(), key=lambda kv: (kv[0][0], order[kv[1][0]])):
        try:
            hits, capped = _match_lines(pattern, text, deadline)
        except TimeoutError:
            timed_out = True
            break
        searched += 1
        if capped:
            truncated = True
        if not hits:
            continue
        total += len(hits)
        shown = hits[:left]
        left -= len(shown)
        if len(shown) < len(hits):
            truncated = True
        lines = [{"line": i + 1, **_snippet(line, spans)} for i, line, spans in shown]
        files_out.append({"path": path, "refs": refs, "count": len(hits), "lines": lines})
    return {
        "files": files_out,
        "lines": total,
        "files_searched": searched,
        "truncated": truncated,
        "timed_out": timed_out,
    }
