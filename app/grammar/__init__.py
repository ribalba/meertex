"""Spelling and grammar while writing (docs/DESIGN.md §17).

* ``checker.py`` holds the per-user settings and their validation, the privacy
  guard, the LanguageTool client, and the check itself with its paragraph
  cache and its budget. meerail's app/grammar.py, ported.
* ``latex.py`` splits a file into paragraphs and turns LaTeX into
  LanguageTool's annotated text, so commands, math and citations are skipped
  and every offset LanguageTool reports is already an offset into the source.

This module stays empty, so the pure parts import without the database.
"""
