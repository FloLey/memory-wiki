"""Run guard and failure reports.

Every dream entry point runs through ``_guarded``: it takes the lock, checks the
API key, and turns a missing key or any exception into a written report rather
than a raw 500.
"""

from __future__ import annotations

import datetime
import os
import traceback

from wiki_server.store import write_files

from .config import DREAM_REPORTS_DIR, _dream_lock


def _no_key_report(day: str, dry: bool) -> tuple[str, str]:
    body = "ANTHROPIC_API_KEY is not set; cannot run the dream. Add it as a secret."
    suffix = "-dryrun" if dry else ""
    rel = f"{DREAM_REPORTS_DIR}/{day}{suffix}.md"
    report = f"# Dream {'dry-run' if dry else ''}, {day}\n\n{body}\n"
    write_files({rel: report}, f"dream: report {day}")
    return rel, report


def _error_report(day: str, dry: bool) -> tuple[str, str]:
    """Capture an unexpected failure into a report instead of crashing the
    request, so the user sees the cause and the dream never returns a raw 500."""
    tb = traceback.format_exc()
    suffix = "-dryrun" if dry else ""
    rel = f"{DREAM_REPORTS_DIR}/{day}{suffix}.md"
    report = (
        f"# Dream {'dry-run' if dry else ''}, {day}\n\n"
        f"Le rêve a échoué. Trace technique :\n\n```\n{tb}\n```\n"
    )
    try:
        write_files({rel: report}, f"dream: error report {day}")
    except Exception:
        pass
    return rel, report


def _guarded(dry: bool, work) -> tuple[str, str]:
    """Run a dream entry point under the lock. Never raises: returns a report if
    the API key is missing or the work fails, so the UI never gets a raw 500."""
    with _dream_lock:
        when = datetime.datetime.now(datetime.timezone.utc)
        day = when.date().isoformat()
        try:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return _no_key_report(day, dry)
            return work(when, day)
        except Exception:
            return _error_report(day, dry)
