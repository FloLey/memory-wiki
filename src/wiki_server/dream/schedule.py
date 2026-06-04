"""Automatic nightly dream.

An in-process scheduler thread (started by the server) runs the dream once a day
after a configured local hour. The mode is chosen by the owner and stored in the
wiki: ``off`` (default), ``dry-run`` (propose, review in the morning), or
``execute`` (apply). "Once a day" is enforced by the presence of the day's report,
so a restart never double-runs and a missed night is caught up when the box is
back. Runs go through the same guard as the manual buttons (shared lock, never
raises).
"""

from __future__ import annotations

import datetime
import json
import logging
import time

from wiki_server.paths import resolve_under_root

from .config import DREAM_REPORTS_DIR
from .pipeline import run_dry_run, run_execute

_log = logging.getLogger(__name__)


def _valid_tz(tz: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
        return True
    except Exception:
        return False

SCHEDULE_FILE = "dream_schedule.json"
MODES = ("off", "dry-run", "execute")
DEFAULT_SCHEDULE = {"mode": "off", "hour": 3, "tz": "Europe/Brussels"}
_POLL_SECONDS = 600


def read_schedule() -> dict:
    """The nightly settings, falling back to the defaults for missing/invalid keys."""
    out = dict(DEFAULT_SCHEDULE)
    path = resolve_under_root(SCHEDULE_FILE)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        if isinstance(data, dict):
            if data.get("mode") in MODES:
                out["mode"] = data["mode"]
            if isinstance(data.get("hour"), int) and 0 <= data["hour"] <= 23:
                out["hour"] = data["hour"]
            if isinstance(data.get("tz"), str) and data["tz"].strip():
                out["tz"] = data["tz"].strip()
    return out


def set_schedule(mode: str, hour, tz: str | None = None) -> dict:
    """Persist the nightly settings, keeping only valid values, and return them."""
    from wiki_server.store import write_file

    current = read_schedule()
    if mode in MODES:
        current["mode"] = mode
    try:
        h = int(hour)
        if 0 <= h <= 23:
            current["hour"] = h
    except (TypeError, ValueError):
        pass
    if isinstance(tz, str) and tz.strip() and _valid_tz(tz.strip()):
        current["tz"] = tz.strip()
    write_file(SCHEDULE_FILE, json.dumps(current, indent=2) + "\n",
               "manual: set nightly dream schedule")
    return current


def _now_local(tz: str | None) -> datetime.datetime:
    """Current time in the configured zone, falling back to UTC if it is unknown
    (e.g. the tz database is missing)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(tz or DEFAULT_SCHEDULE["tz"]))
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc)


def _report_done(day: str, mode: str) -> bool:
    """Has today's run already produced its report?"""
    name = f"{day}.md" if mode == "execute" else f"{day}-dryrun.md"
    return resolve_under_root(f"{DREAM_REPORTS_DIR}/{name}").is_file()


def _due_action(now_local: datetime.datetime, hour: int, mode: str, report_done) -> str | None:
    """The action to run now, or None. Pure (report_done is injected) so it can be
    tested without the clock or the filesystem."""
    if mode not in ("dry-run", "execute"):
        return None
    if now_local.hour < hour:
        return None
    if report_done(now_local.date().isoformat(), mode):
        return None
    return mode


def _tick() -> str | None:
    """One scheduler check: run the dream if it is due. Returns what ran, or None."""
    sched = read_schedule()
    action = _due_action(_now_local(sched["tz"]), sched["hour"], sched["mode"], _report_done)
    if action == "execute":
        run_execute()
    elif action == "dry-run":
        run_dry_run()
    return action


def run_scheduler(poll_seconds: int = _POLL_SECONDS) -> None:
    """Loop forever, checking every ``poll_seconds`` whether the nightly dream is
    due. Started as a daemon thread by the server; never raises out of the loop."""
    while True:
        try:
            _tick()
        except Exception:
            _log.exception("nightly dream scheduler tick failed")
        time.sleep(poll_seconds)
