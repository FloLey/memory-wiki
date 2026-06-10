"""Nightly scheduler: settings round-trip and the due-decision logic."""

import datetime

from wiki_server.dream import schedule


def test_default_schedule_is_off(wiki):
    s = schedule.read_schedule()
    assert s == {"mode": "off", "hour": 3, "tz": "Europe/Brussels",
                 "min_entries": 3, "max_entries": 20}


def test_set_schedule_validates(wiki):
    out = schedule.set_schedule("execute", 5, "Europe/Paris", 4, 30)
    assert out["mode"] == "execute"
    s = schedule.read_schedule()
    assert (s["mode"] == "execute" and s["hour"] == 5 and s["tz"] == "Europe/Paris"
            and s["min_entries"] == 4 and s["max_entries"] == 30)
    # invalid mode, hour, timezone, min and max are ignored, keeping previous values
    schedule.set_schedule("bogus", 99, "Invalid/Zone", 0, 0)
    s = schedule.read_schedule()
    assert (s["mode"] == "execute" and s["hour"] == 5 and s["tz"] == "Europe/Paris"
            and s["min_entries"] == 4 and s["max_entries"] == 30)


def _at(hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 6, 4, hour, 0, tzinfo=datetime.timezone.utc)


def test_due_off_never_runs():
    assert schedule._due_action(_at(3), 3, "off", lambda d, m: False, 99, 3, 20) is None


def test_due_fires_only_at_the_chosen_hour():
    # below the overflow max: only the chosen hour fires, not before nor after
    assert schedule._due_action(_at(2), 3, "execute", lambda d, m: False, 5, 3, 20) is None
    assert schedule._due_action(_at(3), 3, "execute", lambda d, m: False, 5, 3, 20) == "execute"
    assert schedule._due_action(_at(4), 3, "execute", lambda d, m: False, 5, 3, 20) is None


def test_due_overflow_fires_at_any_hour():
    # reaching max_entries fires immediately, whatever the hour
    assert schedule._due_action(_at(11), 3, "execute", lambda d, m: False, 20, 3, 20) == "execute"
    assert schedule._due_action(_at(23), 3, "dry-run", lambda d, m: False, 25, 3, 20) == "dry-run"


def test_due_skips_if_report_already_done():
    # even an overflow does not double-run once the day's report exists
    assert schedule._due_action(_at(3), 3, "execute", lambda d, m: True, 99, 3, 20) is None


def test_due_dry_run_mode():
    assert schedule._due_action(_at(3), 3, "dry-run", lambda d, m: False, 5, 3, 20) == "dry-run"


def test_due_skips_below_min_entries_at_hour():
    assert schedule._due_action(_at(3), 3, "execute", lambda d, m: False, 2, 3, 20) is None
    assert schedule._due_action(_at(3), 3, "execute", lambda d, m: False, 3, 3, 20) == "execute"


def test_stm_count(wiki, write):
    assert schedule._stm_count() == 0
    write("short_term/entries/a.md", "x")
    write("short_term/entries/b.md", "y")
    assert schedule._stm_count() == 2


def test_status_explains_why_not(wiki, write):
    # off by default
    assert schedule.status()["mode"] == "off"
    assert schedule.status()["would_fire"] is False
    assert schedule.status()["reason"] == "mode off"
    # execute + reaching max -> would fire now whatever the clock hour (overflow),
    # so the assertion does not depend on the real wall clock
    schedule.set_schedule("execute", 0, None, 1, 2)
    write("short_term/entries/a.md", "x")
    write("short_term/entries/b.md", "x")
    s = schedule.status()
    assert s["count"] == 2 and s["min_entries"] == 1 and s["max_entries"] == 2
    assert s["would_fire"] is True


def test_status_blocks_below_max(wiki, write):
    # below the overflow max and (almost certainly) outside the chosen hour: blocked
    schedule.set_schedule("execute", 0, None, 5, 50)
    write("short_term/entries/a.md", "x")
    s = schedule.status()
    assert s["would_fire"] is False
    assert s["reason"]  # a non-empty blocking reason


def test_tick_records_heartbeat(wiki, write):
    schedule._LAST_TICK["at"] = None
    schedule.set_schedule("off", 3)
    schedule._tick()
    assert schedule._LAST_TICK["at"] is not None
    assert schedule._LAST_TICK["action"] == "rien"


def test_report_done_checks_the_right_file(wiki, write):
    write("dream_reports/2026-06-04.md", "x")
    assert schedule._report_done("2026-06-04", "execute") is True
    assert schedule._report_done("2026-06-04", "dry-run") is False
    write("dream_reports/2026-06-05-dryrun.md", "x")
    assert schedule._report_done("2026-06-05", "dry-run") is True
