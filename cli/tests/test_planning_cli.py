"""meertex timeline and meertex start (cli/meertex/cmd_planning.py)."""

from datetime import UTC, datetime

from meertex import cli, cmd_planning


def _p(name: str, start: str, end: str) -> dict:
    return {"name": name, "writing": {"start": start, "end": end, "days": 30}}


def test_overlapping_writing_windows_are_found_from_now_on():
    now = datetime(2027, 1, 1, tzinfo=UTC)
    windows = cmd_planning._windows([
        _p("a", "2027-01-01T00:00:00Z", "2027-01-31T00:00:00Z"),
        _p("b", "2027-01-20T00:00:00Z", "2027-02-20T00:00:00Z"),
        _p("c", "2027-01-25T00:00:00Z", "2027-02-01T00:00:00Z"),
        _p("past", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        {"name": "no window", "writing": None},
    ])
    found = [(a.date().isoformat(), b.date().isoformat(), names) for a, b, names in cmd_planning._overlaps(windows, now)]
    assert found == [
        ("2027-01-20", "2027-01-25", ["a", "b"]),
        ("2027-01-25", "2027-01-31", ["a", "b", "c"]),
        ("2027-01-31", "2027-02-01", ["b", "c"]),
    ]


def test_planned_flag_goes_into_the_body():
    parser = cli.build_parser()
    args = parser.parse_args(["new", "--planned", "--conference", "ICT4S", "--year", "2027"])
    assert cli.project_body(args) == {"conference": "ICT4S", "year": 2027, "planned": True}
    args = parser.parse_args(["start", "2027-ict4s", "--title", "Green ICT", "--starter", "basic"])
    assert (args.title, args.starter, args.keep_name) == ("Green ICT", "basic", False)
    args = parser.parse_args(["timeline", "--writing-days", "40"])
    assert args.writing_days == 40
