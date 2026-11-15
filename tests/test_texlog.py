"""Problems in a TeX log (app/texlog.py) and the compile service's status
(compile/service.py), on logs written by hand. No TeX needed."""

import importlib.util

from compile_helpers import SERVICE

from app import texlog

_spec = importlib.util.spec_from_file_location("meertex_compile_service_texlog", SERVICE)
service_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service_module)

# TeX Live 2025 when a longtable breaks across pages. The LaTeX kernel switches
# the error off, TeX logs it anyway, and the line goes on with the page number
# and a file TeX opens. The warning comes from that file.
IGNORED = r"""This is pdfTeX, Version 3.141592653-2.6-1.40.27 (TeX Live 2025) (preloaded format=pdflatex 2025.11.1)
(./main.tex
LaTeX2e <2025-11-01>
./main.tex:131: ignored error: Infinite glue shrinkage found in box being split [1] (./sections/table.tex
LaTeX Warning: Reference `fig:x' on page 2 undefined on input line 3.
)
./main.tex:140: ignored error: Infinite glue shrinkage found in box being split [2]
[3] (./main.aux) )
Output written on main.pdf (3 pages, 14292 bytes).
"""

ERROR = r"""(./main.tex
./main.tex:5: Undefined control sequence.
l.5 \foo

)
"""

FILES = {"main.tex", "sections/table.tex"}


def test_an_ignored_error_is_no_problem():
    problems = texlog.parse(IGNORED, "main.tex", FILES)
    assert problems == [
        {
            "level": "warning",
            "path": "sections/table.tex",
            "line": 3,
            "message": "Reference `fig:x' on page 2 undefined on input line 3.",
        }
    ]


def test_an_ignored_error_does_not_make_the_status_errors():
    assert service_module._ERROR_RE.search(IGNORED) is None
    assert service_module._ERROR_RE.search(ERROR) is not None
    assert texlog.parse(ERROR, "main.tex", FILES) == [
        {"level": "error", "path": "main.tex", "line": 5, "message": "Undefined control sequence."}
    ]
