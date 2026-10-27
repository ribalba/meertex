"""The reference check (docs/DESIGN.md §16).

* ``bibtex.py`` reads .bib files, computes fingerprints and edits fields.
* ``latex.py`` turns LaTeX into text, compares titles and names, and finds
  the \\cite commands in .tex files.
* ``sources.py`` asks doi.org, Crossref, Semantic Scholar, DBLP, arXiv, Open
  Library and the web, through a cache shared by all projects.
* ``checks.py`` turns an entry and what the sources said into findings.
* ``claims.py`` asks Claude whether a cited source supports a sentence.
* ``service.py`` builds the report, runs checks in the background and writes
  into the live document.

This module stays empty, so the pure parts import without the database.
"""
