"""Deadlines and their time zones (DESIGN §5)."""

from datetime import datetime

import pytest

from app import when


def test_time_zones():
    assert when.canonical_tz("aoe") == "AoE"
    assert when.canonical_tz("UTC") == "UTC"
    assert when.canonical_tz("gmt") == "UTC"
    assert when.canonical_tz("UTC+2") == "UTC+2"
    assert when.canonical_tz("utc+02:00") == "UTC+2"
    assert when.canonical_tz("UTC-05:30") == "UTC-5:30"
    assert when.canonical_tz("GMT+1") == "GMT+1"
    assert when.canonical_tz("Europe/Berlin") == "Europe/Berlin"
    for bad in ("Mars/Olympus", "UTC+15", "UTC+2:75", "", "../../etc/passwd", "x" * 300):
        with pytest.raises(ValueError):
            when.canonical_tz(bad)


def test_a_day_is_due_at_its_last_second():
    # AoE is UTC-12, so the day ends at noon UTC the day after.
    assert when.from_date("2027-01-15", "AoE") == datetime(2027, 1, 16, 11, 59, 59)
    assert when.from_date("2027-01-15", "UTC") == datetime(2027, 1, 15, 23, 59, 59)
    assert when.from_date("2027-01-15", "UTC+2") == datetime(2027, 1, 15, 21, 59, 59)
    assert when.from_date("2027-07-15", "Europe/Berlin") == datetime(2027, 7, 15, 21, 59, 59)
    assert when.from_date("2027-01-15", "Europe/Berlin") == datetime(2027, 1, 15, 22, 59, 59)
    with pytest.raises(ValueError):
        when.from_date("15.01.2027", "UTC")


def test_iso_times_with_and_without_offset():
    # GMT+1 means one hour ahead of UTC, as CFPs write it (not Etc/GMT+1).
    assert when.from_iso("2027-01-15T14:00", "GMT+1") == datetime(2027, 1, 15, 13, 0)
    assert when.from_iso("2027-01-15T14:00", "UTC-05:30") == datetime(2027, 1, 15, 19, 30)
    assert when.from_iso("2027-01-15T14:00:00Z", "AoE") == datetime(2027, 1, 15, 14, 0)
    assert when.from_iso("2027-01-15T14:00+02:00", "AoE") == datetime(2027, 1, 15, 12, 0)
    with pytest.raises(ValueError):
        when.from_iso("tomorrow", "UTC")


def test_parse_due():
    assert when.parse_due(None, "2027-01-15", "UTC") == (datetime(2027, 1, 15, 23, 59, 59), True)
    assert when.parse_due("2027-01-15", None, "UTC") == (datetime(2027, 1, 15, 23, 59, 59), True)
    assert when.parse_due("2027-01-15T12:00", None, "UTC") == (datetime(2027, 1, 15, 12, 0), False)
    with pytest.raises(ValueError):
        when.parse_due(None, None, "UTC")


def test_output():
    due = datetime(2027, 1, 16, 11, 59, 59)
    assert when.iso(due) == "2027-01-16T11:59:59Z"
    assert when.iso(None) is None
    assert when.iso_local(due, "AoE") == "2027-01-15T23:59:59-12:00"
    assert when.iso_local(due, "Europe/Berlin") == "2027-01-16T12:59:59+01:00"
    assert when.describe(due, "AoE", True) == "15 Jan 2027"
    assert when.describe(datetime(2027, 3, 1, 12, 0), "UTC+2", False) == "1 Mar 2027 at 14:00 UTC+2"


def test_same_wall_clock_in_another_zone():
    due = when.from_iso("2027-01-15T14:00", "UTC")
    assert when.move_local(due, "UTC", "UTC+2", False) == datetime(2027, 1, 15, 12, 0)
    day = when.from_date("2027-01-15", "UTC")
    assert when.move_local(day, "UTC", "AoE", True) == when.from_date("2027-01-15", "AoE")
