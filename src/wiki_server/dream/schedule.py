"""Automatic nightly dream.

An in-process scheduler thread (started by the server) runs the dream once a day
after a configured local hour. The mode is chosen by the owner and stored in the
wiki: ``off`` (default), ``dry-run`` (propose, review in the morning), or
``execute`` (apply). It only fires when short-term holds at least ``min_entries``
captures, so a thin night never triggers a paid run. "Once a day" is enforced by
the presence of the day's report,
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
# min_entries: only dream automatically when short-term has at least this many
# captures, so a thin night does not trigger a (paid) run.
DEFAULT_SCHEDULE = {"mode": "off", "hour": 3, "tz": "Europe/Brussels", "min_entries": 3}
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
            if isinstance(data.get("min_entries"), int) and data["min_entries"] >= 1:
                out["min_entries"] = data["min_entries"]
    return out


def set_schedule(mode: str, hour, tz: str | None = None, min_entries=None) -> dict:
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
    try:
        m = int(min_entries)
        if m >= 1:
            current["min_entries"] = m
    except (TypeError, ValueError):
        pass
    write_file(SCHEDULE_FILE, json.dumps(current, indent=2) + "\n",
               "manual: set nightly dream schedule")
    return current


def _stm_count() -> int:
    """Number of short-term entries currently waiting to be consolidated."""
    d = resolve_under_root("short_term/entries")
    return len(list(d.glob("*.md"))) if d.is_dir() else 0


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


def _due_action(now_local: datetime.datetime, hour: int, mode: str, report_done,
                entry_count: int, min_entries: int) -> str | None:
    """The action to run now, or None. Pure (report_done and the counts are
    injected) so it can be tested without the clock or the filesystem."""
    if mode not in ("dry-run", "execute"):
        return None
    if now_local.hour < hour:
        return None
    if report_done(now_local.date().isoformat(), mode):
        return None
    if entry_count < max(1, min_entries):
        return None
    return mode


# Heartbeat: the scheduler thread rebinds this each tick (a single atomic
# assignment, so a UI reader never sees a half-updated pair), in-process with the
# server. ``at`` is None until the first tick (i.e. the thread is not running).
_LAST_TICK: dict = {"at": None, "action": None}


def _tick() -> str | None:
    """One scheduler check: run the dream if it is due. Returns what ran, or None."""
    global _LAST_TICK
    sched = read_schedule()
    action = _due_action(_now_local(sched["tz"]), sched["hour"], sched["mode"],
                         _report_done, _stm_count(), sched["min_entries"])
    _LAST_TICK = {"at": _now_local(sched["tz"]).strftime("%Y-%m-%d %H:%M"),
                  "action": action or "rien"}
    if action == "execute":
        _log.info("nightly dream: running execute")
        run_execute()
    elif action == "dry-run":
        _log.info("nightly dream: running dry-run")
        run_dry_run()
    return action


def status() -> dict:
    """Snapshot for the UI: the settings, whether the nightly dream would fire
    right now (and the blocking reason otherwise), the server's local time and
    short-term count, and when the scheduler last checked."""
    sched = read_schedule()
    now = _now_local(sched["tz"])
    count = _stm_count()
    if sched["mode"] not in ("dry-run", "execute"):
        reason = "mode off"
    elif now.hour < sched["hour"]:
        reason = f"avant l'heure ({now.hour:02d}h < {sched['hour']:02d}h {sched['tz']})"
    elif _report_done(now.date().isoformat(), sched["mode"]):
        reason = "rapport du jour déjà présent"
    elif count < max(1, sched["min_entries"]):
        reason = f"pas assez d'entrées ({count} < {sched['min_entries']})"
    else:
        reason = ""
    return {
        "mode": sched["mode"], "hour": sched["hour"], "tz": sched["tz"],
        "min_entries": sched["min_entries"], "now": now.strftime("%H:%M"),
        "count": count, "would_fire": reason == "", "reason": reason,
        "last_tick": _LAST_TICK["at"], "last_action": _LAST_TICK["action"],
    }


def run_scheduler(poll_seconds: int = _POLL_SECONDS) -> None:
    """Loop forever, checking every ``poll_seconds`` whether the nightly dream is
    due. Started as a daemon thread by the server; never raises out of the loop."""
    while True:
        try:
            _tick()
        except Exception:
            _log.exception("nightly dream scheduler tick failed")
        time.sleep(poll_seconds)
