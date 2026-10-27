"""Which local files the sync client never sends (docs/DESIGN.md §10).

The built-in list covers LaTeX's build output and the usual tool folders, the
checkout's ``.meertexignore`` adds the project's own rules in gitignore syntax,
and the main file's PDF is ignored because every coauthor builds their own.
Directories are matched with a trailing slash, the way gitignore needs it, so
the walker can prune a whole ignored tree without looking inside.
"""

import os
from pathlib import Path, PurePosixPath

import pathspec

IGNORE_FILE = ".meertexignore"

DEFAULT_RULES = [
    ".meertex/", ".git/", ".claude/", ".venv/", "venv/", "node_modules/", "__pycache__/",
    ".DS_Store", "*~", "*.swp", ".#*",
    "*.aux", "*.bbl", "*.bcf", "*.blg", "*.dvi", "*.fdb_latexmk", "*.fls", "*.lof", "*.log",
    "*.lot", "*.nav", "*.out", "*.run.xml", "*.snm", "*.synctex.gz", "*.toc", "*.vrb", "*.xdv",
]


class IgnoreRules:
    def __init__(self, root: Path, main_file: str = "main.tex"):
        self.root = root
        self.main_file = main_file or "main.tex"
        self.problem: str | None = None
        self.reload()

    def _stamp(self) -> tuple | None:
        try:
            st = os.stat(self.root / IGNORE_FILE)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def refresh(self) -> bool:
        """Reload when ``.meertexignore`` changed on disk. Returns whether it did."""
        if self._stamp() == self.stamp:
            return False
        self.reload()
        return True

    def reload(self) -> None:
        self.stamp = self._stamp()
        stem = PurePosixPath(self.main_file).with_suffix("").as_posix()
        lines = [*DEFAULT_RULES, "/" + stem + ".pdf"]
        self.problem = None
        try:
            lines += (self.root / IGNORE_FILE).read_text(errors="replace").splitlines()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.problem = f"Cannot read {IGNORE_FILE} ({exc})"
        try:
            self.spec = pathspec.GitIgnoreSpec.from_lines(lines)
        except (ValueError, TypeError) as exc:
            self.problem = f"{IGNORE_FILE} has a rule that does not parse ({exc}). Using the defaults only."
            self.spec = pathspec.GitIgnoreSpec.from_lines([*DEFAULT_RULES, "/" + stem + ".pdf"])

    def ignored(self, rel: str) -> bool:
        """Whether the file at the POSIX path ``rel`` is never sent."""
        return self.spec.match_file(rel)

    def ignored_dir(self, rel: str) -> bool:
        return self.spec.match_file(rel.rstrip("/") + "/")
