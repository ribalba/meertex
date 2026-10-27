"""The document helpers shared by the server and the CLI (app/live/ydoc.py)."""

from pathlib import Path

from pycrdt import Doc

from app.live import ydoc as y

ROOT = Path(__file__).resolve().parent.parent


def test_cli_copy_is_identical():
    # The CLI cannot import the server, so it carries a copy. Copy it again:
    #   cp app/live/ydoc.py cli/meertex/ydoc.py
    assert (ROOT / "cli/meertex/ydoc.py").read_bytes() == (ROOT / "app/live/ydoc.py").read_bytes()


def test_edits_with_multibyte_characters():
    d = Doc()
    fid = y.add_text(d, "main.tex", "Grüße — Welt 😀 end\n")
    y.write_text(d, fid, "Grüße! — Welt 😀😀 ende\nneu")
    assert y.get_entry(d, fid).text == "Grüße! — Welt 😀😀 ende\nneu"


def test_concurrent_edits_in_one_paragraph_merge():
    a = Doc()
    fid = y.add_text(a, "main.tex", "We measure the energy of LLM inference.")
    b = Doc()
    b.apply_update(a.get_update())
    y.write_text(a, fid, "We carefully measure the energy of LLM inference.")
    y.write_text(b, fid, "We measure the energy of LLM inference on GPUs.")
    a.apply_update(b.get_update(a.get_state()))
    b.apply_update(a.get_update(b.get_state()))
    merged = "We carefully measure the energy of LLM inference on GPUs."
    assert y.get_entry(a, fid).text == merged
    assert y.get_entry(b, fid).text == merged


def test_path_clash_lowest_id_wins():
    d = Doc()
    late = y.add_text(d, "intro.tex", "late", file_id="f" * 32)
    y.add_text(d, "intro.tex", "early", file_id="0" * 32)
    files = y.materialize(d)
    assert files["intro.tex"].text == "early"
    assert files[f"intro.conflict-{late[:8]}.tex"].text == "late"


def test_conflict_path_names():
    assert y.conflict_path("a/b.tar.gz", "deadbeef00") == "a/b.conflict-deadbeef.tar.gz"
    assert y.conflict_path(".gitignore", "deadbeef00") == ".gitignore.conflict-deadbeef"


def test_path_rules():
    assert y.path_problem("sections/intro.tex") is None
    for bad in ("", "/etc/passwd", "../x", "a//b", "a/./b", ".meertex/state.bin", ".git/config", "a\\b"):
        assert y.path_problem(bad) is not None, bad


def test_merge_text_replays_our_change():
    assert y.merge_text("a b c d", "a B c d", "a b c D x") == "a B c D x"
    assert y.merge_text("same", "same", "theirs") == "theirs"


def test_put_switches_between_text_and_blob():
    d = Doc()
    first = y.put_blob(d, "fig/plot.png", "ab" * 32, 10)
    second = y.put_text(d, "fig/plot.png", "now text")
    assert first != second
    assert y.find(d, "fig/plot.png").kind == "text"
    assert y.put_text(d, "fig/plot.png", "edited") == second


def test_utf16_positions_round_trip():
    s = "a😀ß€b"
    for i in range(len(s) + 1):
        assert y.from_utf16_index(s, y.utf16_index(s, i)) == i
    assert y.utf16_index(s, 2) == 3
