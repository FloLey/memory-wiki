"""Nightly scheduler: settings round-trip and the due-decision logic."""

import datetime

from wiki_server.dream import schedule


def test_default_schedule_is_off(wiki):
    s = schedule.read_schedule()
    assert s == {"mode": "off", "hour": 3, "tz": "Europe/Brussels"}


def test_set_schedule_validates(wiki):
    assert schedule.set_schedule("execute", 5, "Europe/Paris")["mode"] == "execute"
    s = schedule.read_schedule()
    assert s["mode"] == "execute" and s["hour"] == 5 and s["tz"] == "Europe/Paris"
    # invalid mode, hour and timezone are ignored, keeping the previous values
    schedule.set_schedule("bogus", 99, "Invalid/Zone")
    s = schedule.read_schedule()
    assert s["mode"] == "execute" and s["hour"] == 5 and s["tz"] == "Europe/Paris"


def _at(hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 6, 4, hour, 0, tzinfo=datetime.timezone.utc)


def test_due_off_never_runs():
    assert schedule._due_action(_at(23), 3, "off", lambda d, m: False) is None


def test_due_waits_until_hour():
    assert schedule._due_action(_at(2), 3, "execute", lambda d, m: False) is None
    assert schedule._due_action(_at(3), 3, "execute", lambda d, m: False) == "execute"


def test_due_skips_if_report_already_done():
    assert schedule._due_action(_at(4), 3, "execute", lambda d, m: True) is None


def test_due_dry_run_mode():
    assert schedule._due_action(_at(4), 3, "dry-run", lambda d, m: False) == "dry-run"


def test_report_done_checks_the_right_file(wiki, write):
    write("dream_reports/2026-06-04.md", "x")
    assert schedule._report_done("2026-06-04", "execute") is True
    assert schedule._report_done("2026-06-04", "dry-run") is False
    write("dream_reports/2026-06-05-dryrun.md", "x")
    assert schedule._report_done("2026-06-05", "dry-run") is True
