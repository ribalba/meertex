"""The files sync never sends (docs/DESIGN.md §10)."""

import pytest
from meertex.ignore import IgnoreRules


@pytest.mark.parametrize(
    "name",
    ["main.synctex", "main.synctex.gz", "main.synctex(busy)", "main.synctex.gz(busy)", "sub/paper.synctex(busy)"],
)
def test_synctex_files_are_not_sent(tmp_path, name):
    """TeX writes SyncTeX to a (busy) file while it runs. A build in the
    checkout would otherwise show it to every coauthor for a moment."""
    assert IgnoreRules(tmp_path).ignored(name)


def test_a_file_that_only_sounds_like_it_is_sent(tmp_path):
    assert not IgnoreRules(tmp_path).ignored("synctex.tex")
