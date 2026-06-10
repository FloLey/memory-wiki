"""Temporal items: dated, transient things (todos, reminders, events) that live
until a date or until done, then get archived. Every item has a ``due`` date.
They sit in their own area ``temporal/`` next to long_term/ and short_term/.

Lifecycle by kind:
- event: ``due`` is the end; once it passes the event is over -> expired (hidden).
- todo / reminder (actionable): ``due`` is a deadline. Passing it does NOT mean
  done, so the item stays active and is shown as OVERDUE until it is marked done
  or it has been past due for a long time (the grace period), then it expires.

Status: active -> done (marked) / expired (auto). Nothing is ever deleted.
"""

from __future__ import annotations

import datetime

from wiki_server.paths import resolve_under_root, wiki_root
from wiki_server.store import fm_value, parse_frontmatter, slugify, unique_stem

TEMPORAL_DIR = "temporal"
KINDS = ("todo", "reminder", "event")
# Actionable kinds keep showing as overdue past their due date (a deadline is not
# a completion); they only auto-expire after this many days past due.
ACTIONABLE = ("todo", "reminder")
GRACE_DAYS = 90


def _as_date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def surface_state(meta: dict, today: datetime.date | None = None) -> tuple[bool, bool]:
    """For an active item, return (show, overdue) for prime: a future/undated item
    shows normally; a past-due event is over (hidden); a past-due actionable item
    stays shown, flagged overdue."""
    today = today or datetime.date.today()
    due_d = _as_date(meta.get("due", ""))
    if due_d is None or due_d >= today:
        return True, False
    # Past due: an actionable item is overdue (shown) within the grace window;
    # beyond it (or an event) it is over and hidden, even if no dream expired it.
    if meta.get("type", "todo") in ACTIONABLE and (today - due_d).days <= GRACE_DAYS:
        return True, True
    return False, False


def item_stem(content: str, due: str | None, created: str | None, taken: set[str] | None = None) -> str:
    """A descriptive, unique filename stem like 2026-06-30-voyage-venise. The
    date prefix is the due date when it is a valid ISO date, else created/today,
    so a malformed due never produces a junk prefix or breaks sorting."""
    prefix = None
    if due:
        try:
            datetime.date.fromisoformat(due[:10])
            prefix = due[:10]
        except ValueError:
            pass
    if not prefix:
        prefix = (created or datetime.date.today().isoformat())[:10]
    return unique_stem(TEMPORAL_DIR, f"{prefix}-{slugify(content)}", taken)


def _render_item(kind: str, created: str | None, status: str, due: str | None, body: str) -> str:
    """Render a temporal item's file content (frontmatter + body). Shared by
    build_item (new items) and expire_changes (flipping status)."""
    kind = kind if kind in KINDS else "todo"
    fm = [f"type: {kind}", f"created: {fm_value(created or '')}", f"status: {status}"]
    if due and fm_value(due):
        fm.append(f"due: {fm_value(due)}")
    return "---\n" + "\n".join(fm) + f"\n---\n\n{body.strip()}\n"


def build_item(stem: str, kind: str, due: str | None, content: str,
               created: str | None = None, status: str = "active") -> tuple[str, str]:
    """Build (relative_path, file_content) for a temporal item. Pure, so the
    dream can batch several into one commit."""
    created = created or datetime.date.today().isoformat()
    return f"{TEMPORAL_DIR}/{stem}.md", _render_item(kind, created, status, due, content)


def list_items(active_only: bool = False) -> list[dict]:
    """All temporal items (newest first), each as a dict with path, meta, body."""
    d = resolve_under_root(TEMPORAL_DIR)
    if not d.is_dir():
        return []
    items = []
    for p in sorted(d.glob("*.md"), reverse=True):
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if active_only and meta.get("status", "active") != "active":
            continue
        items.append({"path": p.relative_to(wiki_root()).as_posix(), "meta": meta, "body": body.strip()})
    return items


def expire_changes(today: str | None = None) -> dict[str, str]:
    """Return {path: new_content} for active items to expire, flipping them to
    ``expired``. An event expires once past its end date; an actionable item
    (todo/reminder) only once it is more than ``GRACE_DAYS`` past due (before that
    it stays active and overdue). Pure: the caller commits."""
    today_d = _as_date(today) or datetime.date.today()
    changes: dict[str, str] = {}
    for item in list_items():
        meta, body = item["meta"], item["body"]
        if meta.get("status", "active") != "active":
            continue
        # Only act on a real ISO due; a malformed due (e.g. "10 au 15 juin") never
        # expires (it would compare arbitrarily).
        due_d = _as_date(meta.get("due", ""))
        if due_d is None or due_d >= today_d:
            continue
        kind = meta.get("type", "todo")
        if kind in ACTIONABLE and (today_d - due_d).days <= GRACE_DAYS:
            continue  # overdue but kept until done or long-past
        changes[item["path"]] = _render_item(
            kind, meta.get("created", ""), "expired", meta.get("due", ""), body)
    return changes


def mark_done(rel: str) -> bool:
    """Mark a temporal item done (status: done) so it leaves the active list.
    Returns whether the item existed."""
    from wiki_server.store import write_file

    path = resolve_under_root(rel)
    if not path.is_file():
        return False
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    content = _render_item(meta.get("type", "todo"), meta.get("created", ""),
                           "done", meta.get("due", ""), body)
    write_file(rel, content, f"manual: mark done {path.stem}")
    return True
