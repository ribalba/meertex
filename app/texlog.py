"""Problems in a TeX log (docs/DESIGN.md §14).

The editor lists the errors, warnings and bad boxes of the last compile, and
clicking one opens the file at its line. TeX does not say which file a warning
belongs to. It says which files it opens and closes, as ``(./intro.tex`` and a
later ``)``, so the parser keeps that stack while it reads, the way Overleaf's
log parser does. Parentheses in other text are counted on their own, so a
message like "(see the transcript file)" does not close a file.

The compile service runs TeX with ``max_print_line=10000``, so a message or a
path is never wrapped in the middle. Its logs end with latexmk's last lines
after a marker line. The parser reads only the service's own messages there
(a bibliography outside the project, say), because latexmk repeats TeX's.
"""

import posixpath
import re

# compile/service.py writes this line before latexmk's output, and its guards
# start their lines with GUARD_PREFIX (a bibliography outside the project).
LATEXMK_MARK = "=== latexmk (the last lines of its output) ==="
GUARD_PREFIX = "meertex-compile: "
MAX_PROBLEMS = 1000

# -file-line-error: "./sections/intro.tex:12: Undefined control sequence."
_FILE_LINE_ERROR = re.compile(r"^(?P<file>\S(?:[^:]*?)\.[A-Za-z0-9]+):(?P<line>\d+): (?P<message>.*)$")
_BANG_ERROR = re.compile(r"^! (?P<message>.*)$")
_CONTEXT_LINE = re.compile(r"^l\.(\d+)")
_WARNING = re.compile(r"^(?:(?:LaTeX|Package|Class|Module) (?:[\w.@-]+ )?|LaTeX Font |pdfTeX |)[Ww]arning: (?P<message>.*)$")
_CONTINUATION = re.compile(r"^(?:\([\w.@-]+\)\s+| {4,})(?P<rest>\S.*)$")
_INPUT_LINE = re.compile(r"on input line (\d+)")
_BADBOX = re.compile(r"^(?:Overfull|Underfull) \\[hv]box .*")
_BADBOX_LINES = re.compile(r"at lines? (\d+)")
# A file TeX opens: "(./main.tex", "(/usr/share/.../article.cls" or, for XeTeX
# and LuaTeX with a space in the name, '("./my file.tex"'.
_OPENED = re.compile(r'(?:"(?P<quoted>[^"]+)"|(?P<plain>(?:\.{0,2}/)?[^\s()"\[\]{}<>]*\.[A-Za-z0-9]+))')
_PARENS = re.compile(r"[()]")


class _Files:
    """The stack of files TeX has open, and parentheses that are not files."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.other = 0

    @property
    def current(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def scan(self, line: str) -> None:
        pos = 0
        while True:
            m = _PARENS.search(line, pos)
            if m is None:
                return
            pos = m.end()
            if m.group() == "(":
                opened = _OPENED.match(line, pos)
                if opened:
                    self.stack.append(opened.group("quoted") or opened.group("plain"))
                    pos = opened.end()
                else:
                    self.other += 1
            elif self.other:
                self.other -= 1
            elif self.stack:
                self.stack.pop()


def project_path(name: str | None, main_dir: str, files: set[str]) -> str | None:
    """A path from the log as a path of the project, or None when it is a file
    of TeX Live or a file TeX made (main.aux). TeX runs in the main file's
    folder, so relative paths start there."""
    if not name:
        return None
    if name.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(main_dir, name))
    return path if path in files else None


def parse(log: str, main_file: str, files: set[str]) -> list[dict]:
    """``[{level, path, line, message}]`` in the order of the log. ``level`` is
    error, warning or badbox. ``path`` is relative to the project, or None."""
    main_dir = posixpath.dirname(main_file)
    problems: list[dict] = []
    seen: set[tuple] = set()
    stack = _Files()

    def add(level: str, file: str | None, line: int | None, message: str) -> None:
        path = project_path(file, main_dir, files)
        key = (level, path, line if path else None, message)
        if key in seen or len(problems) >= MAX_PROBLEMS:
            return
        seen.add(key)
        problems.append({"level": level, "path": path, "line": line if path else None, "message": message})

    lines = log.split("\n")
    i = 0
    while i < len(lines):
        text = lines[i].rstrip("\r")
        i += 1
        if text == LATEXMK_MARK:
            for rest in lines[i:]:
                if rest.startswith(GUARD_PREFIX):
                    add("error", None, None, rest[len(GUARD_PREFIX) :].strip())
            break

        m = _FILE_LINE_ERROR.match(text)
        if m and not text.startswith(("(", "[", " ")):
            add("error", m["file"], int(m["line"]), m["message"].strip())
            i = _skip_block(lines, i)
            continue

        m = _BANG_ERROR.match(text)
        if m:
            # Without -file-line-error's form, the line comes after "l.".
            line = None
            j = i
            while j < len(lines) and lines[j].strip() and j < i + 20:
                ctx = _CONTEXT_LINE.match(lines[j])
                if ctx:
                    line = int(ctx[1])
                    break
                j += 1
            add("error", stack.current, line, m["message"].strip())
            i = _skip_block(lines, i)
            continue

        m = _WARNING.match(text)
        if m:
            message = m["message"].strip()
            while i < len(lines) and (cont := _CONTINUATION.match(lines[i].rstrip("\r"))):
                message += " " + cont["rest"].strip()
                i += 1
            found = _INPUT_LINE.search(message)
            add("warning", stack.current if found else None, int(found[1]) if found else None, message)
            continue

        m = _BADBOX.match(text)
        if m:
            found = _BADBOX_LINES.search(text)
            add("badbox", stack.current if found else None, int(found[1]) if found else None, text.strip())
            # TeX shows the box's contents next, which is document text with
            # parentheses of its own, up to an empty line.
            i = _skip_block(lines, i)
            continue

        stack.scan(text)
    return problems


def _skip_block(lines: list[str], i: int) -> int:
    """The index after the next empty line: an error's context and help, or a
    bad box's contents, are document text and must not move the file stack."""
    while i < len(lines) and lines[i].strip():
        i += 1
    return i
